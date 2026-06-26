"""Write validated tokens into cc-switch SQLite database.

Handles providers, provider_endpoints, and provider_health tables.
Never overwrites manually-configured providers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

from validator import ValidationResult, Protocol

log = logging.getLogger(__name__)


class CcSwitchWriter:
    """Write auto-discovered providers into cc-switch.db."""

    def __init__(self, db_path: str, auto_category: str = "auto_discovered", never_overwrite: bool = True):
        self.db_path = db_path
        self.auto_category = auto_category
        self.never_overwrite = never_overwrite

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def write_validated(self, results: list[ValidationResult]) -> int:
        """Write validated results to cc-switch. Returns count of providers written."""
        if not results:
            return 0

        con = self._connect()
        cur = con.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        now_ms = int(time.time() * 1000)
        written = 0

        try:
            for vr in results:
                provider_id = vr.token.uid
                app_type = vr.app_type

                # Check if this provider already exists
                existing = cur.execute(
                    "SELECT id, category FROM providers WHERE id = ? AND app_type = ?",
                    (provider_id, app_type),
                ).fetchone()

                if existing:
                    cat = existing["category"] or ""
                    if self.never_overwrite and cat != self.auto_category:
                        log.info("  Skipping %s — manually configured (category=%s)", provider_id, cat)
                        continue
                    # Update existing auto-discovered provider
                    self._update_provider(cur, provider_id, app_type, vr, now_ms)
                    self._update_health(cur, provider_id, app_type, vr, now_iso)
                    log.info("  Updated %s [%s]", provider_id, app_type)
                else:
                    # Insert new provider
                    self._insert_provider(cur, provider_id, app_type, vr, now_ms)
                    self._insert_endpoint(cur, provider_id, app_type, vr.token.base_url, now_ms)
                    self._insert_health(cur, provider_id, app_type, vr, now_iso)
                    log.info("  Inserted %s [%s] %s", provider_id, app_type, vr.token.base_url)

                written += 1

            con.commit()
        except Exception as e:
            log.error("DB write error: %s", e)
            con.rollback()
        finally:
            con.close()

        return written

    def cleanup_expired(self, max_failures: int = 3) -> int:
        """Mark providers with too many consecutive failures as expired."""
        con = self._connect()
        cur = con.cursor()
        removed = 0
        try:
            rows = cur.execute(
                "SELECT provider_id, app_type, consecutive_failures "
                "FROM provider_health WHERE consecutive_failures >= ?",
                (max_failures,),
            ).fetchall()
            for row in rows:
                pid = row["provider_id"]
                at = row["app_type"]
                # Only touch auto-discovered providers
                prov = cur.execute(
                    "SELECT category FROM providers WHERE id = ? AND app_type = ?",
                    (pid, at),
                ).fetchone()
                if prov and (prov["category"] or "") == self.auto_category:
                    cur.execute(
                        "UPDATE providers SET category = ? WHERE id = ? AND app_type = ?",
                        ("expired", pid, at),
                    )
                    log.info("  Marked expired: %s [%s]", pid, at)
                    removed += 1
            con.commit()
        except Exception as e:
            log.error("Cleanup error: %s", e)
        finally:
            con.close()
        return removed

    def list_auto_providers(self) -> list[dict]:
        """List all auto-discovered providers."""
        con = self._connect()
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, app_type, name, category, settings_config FROM providers WHERE category = ?",
            (self.auto_category,),
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "app_type": r["app_type"],
                "name": r["name"],
                "category": r["category"],
            })
        con.close()
        return result

    # -- Private helpers -----------------------------------------------

    def _make_name(self, vr: ValidationResult) -> str:
        from urllib.parse import urlparse
        host = urlparse(vr.token.base_url).netloc or vr.token.base_url[:40]
        source_short = vr.token.source.split("/")[-1] if "/" in vr.token.source else vr.token.source
        return f"Auto | {host} ({source_short})"

    def _make_settings_config(self, vr: ValidationResult) -> str:
        """Build settings_config JSON matching cc-switch format for each app_type."""
        at = vr.app_type
        base = vr.token.base_url.rstrip("/")
        key = vr.token.api_key
        models = vr.discovered_models or vr.token.raw_models or []
        default_model = models[0] if models else "gpt-4o"

        if at == "codex":
            provider_name = f"auto_{vr.token.uid}"
            config_toml = (
                f'model_provider = "{provider_name}"\n'
                f'model = "{default_model}"\n'
                f'disable_response_storage = true\n\n'
                f'[model_providers]\n'
                f'[model_providers.{provider_name}]\n'
                f'name = "{provider_name}"\n'
                f'base_url = "{base}"\n'
                f'wire_api = "responses"\n'
                f'requires_openai_auth = true\n'
            )
            return json.dumps({
                "auth": {"OPENAI_API_KEY": key},
                "config": config_toml,
            }, ensure_ascii=False)

        if at == "claude":
            return json.dumps({
                "env": {"ANTHROPIC_API_KEY": key},
                "baseUrl": base,
            }, ensure_ascii=False)

        # openclaw
        model_list = []
        for m in models[:10]:
            model_list.append({"id": m, "name": m, "contextWindow": 128000})
        if not model_list:
            model_list = [{"id": default_model, "name": default_model, "contextWindow": 128000}]

        return json.dumps({
            "baseUrl": base,
            "apiKey": key,
            "api": "openai-chat",
            "models": model_list,
        }, ensure_ascii=False)

    def _insert_provider(self, cur, pid: str, app_type: str, vr: ValidationResult, now_ms: int):
        name = self._make_name(vr)
        cfg = self._make_settings_config(vr)
        cur.execute(
            "INSERT INTO providers (id, app_type, name, settings_config, category, created_at, sort_index, is_current, in_failover_queue, provider_type) "
            "VALUES (?, ?, ?, ?, ?, ?, 999, 0, 0, 'auto')",
            (pid, app_type, name, cfg, self.auto_category, now_ms),
        )

    def _update_provider(self, cur, pid: str, app_type: str, vr: ValidationResult, now_ms: int):
        cfg = self._make_settings_config(vr)
        cur.execute(
            "UPDATE providers SET settings_config = ?, sort_index = 999 WHERE id = ? AND app_type = ?",
            (cfg, pid, app_type),
        )

    def _insert_endpoint(self, cur, pid: str, app_type: str, url: str, now_ms: int):
        cur.execute(
            "INSERT OR IGNORE INTO provider_endpoints (provider_id, app_type, url, added_at) VALUES (?, ?, ?, ?)",
            (pid, app_type, url, now_ms),
        )

    def _insert_health(self, cur, pid: str, app_type: str, vr: ValidationResult, now_iso: str):
        cur.execute(
            "INSERT OR REPLACE INTO provider_health "
            "(provider_id, app_type, is_healthy, consecutive_failures, last_success_at, last_failure_at, last_error, updated_at) "
            "VALUES (?, ?, ?, 0, ?, NULL, NULL, ?)",
            (pid, app_type, 1 if vr.is_healthy else 0, now_iso, now_iso),
        )

    def _update_health(self, cur, pid: str, app_type: str, vr: ValidationResult, now_iso: str):
        if vr.is_healthy:
            cur.execute(
                "UPDATE provider_health SET is_healthy = 1, consecutive_failures = 0, last_success_at = ?, updated_at = ? "
                "WHERE provider_id = ? AND app_type = ?",
                (now_iso, now_iso, pid, app_type),
            )
        else:
            cur.execute(
                "UPDATE provider_health SET is_healthy = 0, consecutive_failures = consecutive_failures + 1, "
                "last_failure_at = ?, last_error = ?, updated_at = ? "
                "WHERE provider_id = ? AND app_type = ?",
                (now_iso, vr.error[:200] if vr.error else "", now_iso, pid, app_type),
            )
