"""Write validated tokens into cc-switch SQLite database.

Handles providers, provider_endpoints, and provider_health tables.
Never overwrites manually-configured providers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import tomllib
from datetime import datetime, timezone

from sources.base import DiscoveredToken
from validator import ValidationResult

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
        """Backwards-compatible wrapper that only reports healthy writes."""
        return self.sync_results(results)["healthy_written"]

    def sync_results(self, results: list[ValidationResult]) -> dict[str, int]:
        """Write healthy providers and persist failure health for managed providers."""
        summary = {
            "healthy_written": 0,
            "failure_updates": 0,
            "skipped_manual": 0,
            "skipped_unhealthy_new": 0,
        }
        if not results:
            return summary

        con = self._connect()
        cur = con.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        now_ms = int(time.time() * 1000)

        try:
            self._cleanup_superseded_providers(cur)
            for vr in results:
                provider_id = vr.provider_id
                app_type = vr.app_type
                existing = cur.execute(
                    "SELECT id, category FROM providers WHERE id = ? AND app_type = ?",
                    (provider_id, app_type),
                ).fetchone()

                if existing:
                    category = existing["category"] or ""
                    if self.never_overwrite and not self._is_auto_managed_category(category):
                        log.info("  Skipping %s — manually configured (category=%s)", provider_id, category)
                        summary["skipped_manual"] += 1
                        continue

                    if vr.is_healthy:
                        self._update_provider(cur, provider_id, app_type, vr, now_ms)
                        self._insert_endpoint(cur, provider_id, app_type, vr.token.base_url, now_ms)
                        self._upsert_health(cur, provider_id, app_type, vr, now_iso)
                        self._expire_stale_app_types(cur, provider_id, app_type)
                        log.info("  Updated %s [%s]", provider_id, app_type)
                        summary["healthy_written"] += 1
                    else:
                        self._upsert_health(cur, provider_id, app_type, vr, now_iso)
                        log.info("  Recorded failure for %s [%s]: %s", provider_id, app_type, vr.error)
                        summary["failure_updates"] += 1
                    continue

                if not vr.is_healthy:
                    log.info("  Skip unhealthy new provider %s [%s]: %s", provider_id, app_type, vr.error)
                    summary["skipped_unhealthy_new"] += 1
                    continue

                self._insert_provider(cur, provider_id, app_type, vr, now_ms)
                self._insert_endpoint(cur, provider_id, app_type, vr.token.base_url, now_ms)
                self._upsert_health(cur, provider_id, app_type, vr, now_iso)
                self._expire_stale_app_types(cur, provider_id, app_type)
                log.info("  Inserted %s [%s] %s", provider_id, app_type, vr.token.base_url)
                summary["healthy_written"] += 1

            con.commit()
        except Exception as e:
            log.error("DB write error: %s", e)
            con.rollback()
            summary = {key: 0 for key in summary}
        finally:
            con.close()

        return summary

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

    def list_expired_providers(self) -> list[dict]:
        """List providers already marked as expired."""
        con = self._connect()
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, app_type, name, category FROM providers WHERE category = ?",
            ("expired",),
        ).fetchall()
        result = [
            {
                "id": row["id"],
                "app_type": row["app_type"],
                "name": row["name"],
                "category": row["category"],
            }
            for row in rows
        ]
        con.close()
        return result

    def delete_provider(self, provider_id: str, app_type: str) -> bool:
        """Physically remove a provider and its related rows."""
        con = self._connect()
        cur = con.cursor()
        deleted = False
        try:
            existing = cur.execute(
                "SELECT id FROM providers WHERE id = ? AND app_type = ?",
                (provider_id, app_type),
            ).fetchone()
            if not existing:
                return False

            cur.execute(
                "DELETE FROM provider_health WHERE provider_id = ? AND app_type = ?",
                (provider_id, app_type),
            )
            cur.execute(
                "DELETE FROM provider_endpoints WHERE provider_id = ? AND app_type = ?",
                (provider_id, app_type),
            )
            cur.execute(
                "DELETE FROM providers WHERE id = ? AND app_type = ?",
                (provider_id, app_type),
            )
            con.commit()
            deleted = True
        except Exception as e:
            log.error("Delete provider error for %s [%s]: %s", provider_id, app_type, e)
            con.rollback()
        finally:
            con.close()
        return deleted

    def purge_invalid_providers(
        self,
        results: list[ValidationResult],
        *,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Remove auto-managed providers that failed validation."""
        summary = {
            "validated": len(results),
            "kept_healthy": 0,
            "removed_unhealthy": 0,
            "skipped_manual": 0,
        }
        for vr in results:
            category = str(vr.token.extra.get("category") or self.auto_category)
            if self.never_overwrite and not self._is_auto_managed_category(category):
                summary["skipped_manual"] += 1
                continue

            if vr.is_healthy:
                summary["kept_healthy"] += 1
                continue

            reason = vr.error or f"HTTP {vr.http_status}" or "validation_failed"
            if dry_run:
                log.info(
                    "  [dry-run] Would remove %s [%s]: %s",
                    vr.provider_id,
                    vr.app_type,
                    reason,
                )
                summary["removed_unhealthy"] += 1
                continue

            if self.delete_provider(vr.provider_id, vr.app_type):
                log.info("  Removed %s [%s]: %s", vr.provider_id, vr.app_type, reason)
                summary["removed_unhealthy"] += 1
        return summary

    def purge_already_expired(self, *, dry_run: bool = False) -> int:
        """Physically delete providers already marked with category=expired."""
        providers = self.list_expired_providers()
        removed = 0
        for provider in providers:
            pid = provider["id"]
            app_type = provider["app_type"]
            if dry_run:
                log.info("  [dry-run] Would delete expired %s [%s] %s", pid, app_type, provider["name"])
                removed += 1
                continue
            if self.delete_provider(pid, app_type):
                log.info("  Deleted expired %s [%s] %s", pid, app_type, provider["name"])
                removed += 1
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

    def list_managed_provider_tokens(self) -> list[DiscoveredToken]:
        """Rebuild tokens from auto-managed provider configs for re-validation."""
        con = self._connect()
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, app_type, name, category, settings_config "
            "FROM providers WHERE category = ?",
            (self.auto_category,),
        ).fetchall()

        # 同一 provider id 只保留一条；优先 Codex 以便复检时先探测 /responses
        deduped: dict[str, sqlite3.Row] = {}
        app_type_priority = {"codex": 0, "openclaw": 1, "claude": 2}
        for row in rows:
            existing = deduped.get(row["id"])
            if existing is None:
                deduped[row["id"]] = row
                continue
            if app_type_priority.get(row["app_type"], 9) < app_type_priority.get(existing["app_type"], 9):
                deduped[row["id"]] = row

        tokens: list[DiscoveredToken] = []
        for row in deduped.values():
            token = self._row_to_token(row)
            if token:
                tokens.append(token)

        con.close()
        return tokens

    # -- Private helpers -----------------------------------------------

    def _make_name(self, vr: ValidationResult) -> str:
        from urllib.parse import urlparse
        host = urlparse(vr.token.base_url).netloc or vr.token.base_url[:40]
        source_short = vr.token.source.split("/")[-1] if "/" in vr.token.source else vr.token.source
        return f"Auto | {host} ({source_short})"

    def _is_auto_managed_category(self, category: str) -> bool:
        return category in {self.auto_category, "expired"}

    @staticmethod
    def _claude_base_url(base: str) -> str:
        """cc-switch Claude 配置使用不含 /v1 的根 URL，由客户端自行拼接路径。"""
        normalized = base.rstrip("/")
        if normalized.lower().endswith("/v1"):
            return normalized[:-3]
        return normalized

    @staticmethod
    def _validation_base_url(base: str) -> str:
        """验证器探测需要带 /v1 的 base URL。"""
        normalized = base.rstrip("/")
        if normalized.lower().endswith("/v1"):
            return normalized
        return f"{normalized}/v1"

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
                "env": {
                    "ANTHROPIC_API_KEY": key,
                    "ANTHROPIC_BASE_URL": self._claude_base_url(base),
                },
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

    def _expire_stale_app_types(self, cur, provider_id: str, app_type: str) -> None:
        """Remove auto-managed rows for the same provider id but a different app_type."""
        rows = cur.execute(
            "SELECT app_type FROM providers WHERE id = ? AND app_type != ? AND category = ?",
            (provider_id, app_type, self.auto_category),
        ).fetchall()
        for row in rows:
            stale_type = row["app_type"]
            cur.execute(
                "DELETE FROM provider_health WHERE provider_id = ? AND app_type = ?",
                (provider_id, stale_type),
            )
            cur.execute(
                "DELETE FROM provider_endpoints WHERE provider_id = ? AND app_type = ?",
                (provider_id, stale_type),
            )
            cur.execute(
                "DELETE FROM providers WHERE id = ? AND app_type = ?",
                (provider_id, stale_type),
            )
            log.info("  Removed superseded %s [%s]", provider_id, stale_type)

    def _cleanup_superseded_providers(self, cur) -> None:
        """Repair legacy Claude configs that used top-level baseUrl."""
        claude_rows = cur.execute(
            "SELECT id, settings_config FROM providers WHERE app_type = 'claude' AND category = ?",
            (self.auto_category,),
        ).fetchall()
        for row in claude_rows:
            try:
                payload = json.loads(row["settings_config"])
            except json.JSONDecodeError:
                continue
            legacy_url = payload.get("baseUrl")
            env = payload.get("env", {})
            if not legacy_url or env.get("ANTHROPIC_BASE_URL"):
                continue
            env["ANTHROPIC_BASE_URL"] = self._claude_base_url(legacy_url)
            payload["env"] = env
            payload.pop("baseUrl", None)
            cur.execute(
                "UPDATE providers SET settings_config = ? WHERE id = ? AND app_type = 'claude'",
                (json.dumps(payload, ensure_ascii=False), row["id"]),
            )
            log.info("  Repaired legacy claude config for %s", row["id"])

    def _insert_provider(self, cur, pid: str, app_type: str, vr: ValidationResult, now_ms: int):
        name = self._make_name(vr)
        cfg = self._make_settings_config(vr)
        cur.execute(
            "INSERT INTO providers (id, app_type, name, settings_config, category, created_at, sort_index, is_current, in_failover_queue, provider_type) "
            "VALUES (?, ?, ?, ?, ?, ?, 999, 0, 0, 'auto')",
            (pid, app_type, name, cfg, self.auto_category, now_ms),
        )

    def _update_provider(self, cur, pid: str, app_type: str, vr: ValidationResult, now_ms: int):
        name = self._make_name(vr)
        cfg = self._make_settings_config(vr)
        cur.execute(
            "UPDATE providers SET name = ?, settings_config = ?, category = ?, sort_index = 999 "
            "WHERE id = ? AND app_type = ?",
            (name, cfg, self.auto_category, pid, app_type),
        )

    def _insert_endpoint(self, cur, pid: str, app_type: str, url: str, now_ms: int):
        cur.execute(
            "INSERT OR IGNORE INTO provider_endpoints (provider_id, app_type, url, added_at) VALUES (?, ?, ?, ?)",
            (pid, app_type, url, now_ms),
        )

    def _upsert_health(self, cur, pid: str, app_type: str, vr: ValidationResult, now_iso: str):
        exists = cur.execute(
            "SELECT 1 FROM provider_health WHERE provider_id = ? AND app_type = ?",
            (pid, app_type),
        ).fetchone()
        if exists:
            self._update_health(cur, pid, app_type, vr, now_iso)
        else:
            self._insert_health(cur, pid, app_type, vr, now_iso)

    def _insert_health(self, cur, pid: str, app_type: str, vr: ValidationResult, now_iso: str):
        cur.execute(
            "INSERT INTO provider_health "
            "(provider_id, app_type, is_healthy, consecutive_failures, last_success_at, last_failure_at, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                app_type,
                1 if vr.is_healthy else 0,
                0 if vr.is_healthy else 1,
                now_iso if vr.is_healthy else None,
                None if vr.is_healthy else now_iso,
                None if vr.is_healthy else (vr.error[:200] if vr.error else ""),
                now_iso,
            ),
        )

    def _update_health(self, cur, pid: str, app_type: str, vr: ValidationResult, now_iso: str):
        if vr.is_healthy:
            cur.execute(
                "UPDATE provider_health SET is_healthy = 1, consecutive_failures = 0, "
                "last_success_at = ?, last_error = NULL, updated_at = ? "
                "WHERE provider_id = ? AND app_type = ?",
                (now_iso, now_iso, pid, app_type),
            )
            return

        cur.execute(
            "UPDATE provider_health SET is_healthy = 0, consecutive_failures = consecutive_failures + 1, "
            "last_failure_at = ?, last_error = ?, updated_at = ? "
            "WHERE provider_id = ? AND app_type = ?",
            (now_iso, vr.error[:200] if vr.error else "", now_iso, pid, app_type),
        )

    def _row_to_token(self, row: sqlite3.Row) -> DiscoveredToken | None:
        try:
            payload = json.loads(row["settings_config"])
        except Exception as e:
            log.warning("Failed to parse provider config %s: %s", row["id"], e)
            return None

        base_url = ""
        api_key = ""
        models: list[str] = []
        app_type = row["app_type"]

        try:
            if app_type == "openclaw":
                base_url = payload.get("baseUrl", "")
                api_key = payload.get("apiKey", "")
                models = [m.get("id", "") for m in payload.get("models", []) if isinstance(m, dict)]
            elif app_type == "claude":
                env = payload.get("env", {})
                base_url = env.get("ANTHROPIC_BASE_URL") or payload.get("baseUrl", "")
                api_key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN", "")
                base_url = self._validation_base_url(base_url) if base_url else ""
            elif app_type == "codex":
                api_key = payload.get("auth", {}).get("OPENAI_API_KEY", "")
                config_text = payload.get("config", "")
                config_data = tomllib.loads(config_text)
                provider_map = config_data.get("model_providers", {})
                provider_cfg = next(iter(provider_map.values()), {}) if provider_map else {}
                base_url = provider_cfg.get("base_url", "")
                model = config_data.get("model", "")
                if isinstance(model, str) and model:
                    models = [model]
        except Exception as e:
            log.warning("Failed to rebuild provider token %s: %s", row["id"], e)
            return None

        if not base_url or not api_key:
            log.warning("Skip provider %s due to incomplete config", row["id"])
            return None

        return DiscoveredToken(
            source=f"ccswitch:{row['id']}",
            base_url=base_url,
            api_key=api_key,
            raw_models=[model for model in models if model],
            extra={"provider_id": row["id"], "app_type": app_type, "category": row["category"]},
        )
