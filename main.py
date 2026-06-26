"""Free Token API Collector — main entry point.

Usage:
    python main.py              # 一次性采集 + 验证 + 写入 cc-switch
    python main.py --daemon     # 后台守护模式（定时刷新）
    python main.py --validate   # 仅验证已有 auto provider
    python main.py --clean      # 清理过期/失败 token
    python main.py --list       # 列出当前自动发现的 provider
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import yaml

from logger import setup_logging
from db_writer import CcSwitchWriter
from validator import TokenValidator
from sources import GitHubReadmeSource, GitHubSearchSource, WebAggregatorSource, DiscoveredToken

log = logging.getLogger("collector")

# ── helpers ──────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    p = Path(__file__).parent / path
    if not p.exists():
        log.error("Config not found: %s", p)
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def collect_all(config: dict) -> list[DiscoveredToken]:
    """Run all configured sources and return merged, deduplicated tokens."""
    network = config.get("network", {})
    sources_cfg = config.get("sources", [])
    gh_cfg = config.get("github", {})

    all_tokens: list[DiscoveredToken] = []
    seen_uids: set[str] = set()

    for src_cfg in sources_cfg:
        src_type = src_cfg.get("type", "")

        # Inject shared fields
        src_cfg["_github_token"] = gh_cfg.get("token", "")
        src_cfg["_request_delay"] = gh_cfg.get("request_delay_seconds", 2)

        try:
            if src_type == "github_readme":
                src = GitHubReadmeSource(src_cfg, network)
            elif src_type == "github_search":
                src = GitHubSearchSource(src_cfg, network)
            elif src_type == "web_aggregator":
                src = WebAggregatorSource(src_cfg, network)
            else:
                log.warning("Unknown source type: %s", src_type)
                continue

            log.info("━━━ Collecting from %s ━━━", src.name)
            tokens = src.collect()
            for t in tokens:
                if t.uid not in seen_uids:
                    seen_uids.add(t.uid)
                    all_tokens.append(t)
        except Exception as e:
            log.error("Source %s failed: %s", src_type, e)

    log.info("Total unique tokens collected: %d", len(all_tokens))
    return all_tokens


def validate_tokens(tokens: list[DiscoveredToken], config: dict) -> list:
    """Validate tokens and return healthy results."""
    network = config.get("network", {})
    val_cfg = config.get("validator", {})
    validator = TokenValidator(network, val_cfg)

    log.info("━━━ Validating %d tokens ━━━", len(tokens))
    results = asyncio.run(validator.validate_all(tokens))
    log.info("Healthy tokens: %d / %d", len(results), len(tokens))
    return results


def write_to_ccswitch(results: list, config: dict) -> int:
    """Write validated results to cc-switch DB."""
    cs_cfg = config.get("ccswitch", {})
    writer = CcSwitchWriter(
        db_path=cs_cfg.get("db_path", r"C:\Users\Administrator\.cc-switch\cc-switch.db"),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
        never_overwrite=cs_cfg.get("never_overwrite", True),
    )
    log.info("━━━ Writing to cc-switch ━━━")
    count = writer.write_validated(results)
    log.info("Wrote %d providers to cc-switch", count)
    return count


# ── commands ─────────────────────────────────────────────────────────

def cmd_run(config: dict) -> None:
    """Full pipeline: collect → validate → write."""
    t0 = time.time()
    tokens = collect_all(config)
    if not tokens:
        log.warning("No tokens collected from any source.")
        return

    results = validate_tokens(tokens, config)
    if not results:
        log.warning("No tokens passed validation.")
        return

    count = write_to_ccswitch(results, config)
    elapsed = time.time() - t0
    log.info("Done in %.1fs — %d providers written to cc-switch", elapsed, count)


def cmd_daemon(config: dict) -> None:
    """Daemon mode with periodic refresh."""
    from scheduler import run_daemon

    def collect_and_write():
        tokens = collect_all(config)
        if tokens:
            results = validate_tokens(tokens, config)
            if results:
                write_to_ccswitch(results, config)

    def validate_existing():
        cs_cfg = config.get("ccswitch", {})
        writer = CcSwitchWriter(
            db_path=cs_cfg.get("db_path", r"C:\Users\Administrator\.cc-switch\cc-switch.db"),
            auto_category=cs_cfg.get("auto_category", "auto_discovered"),
        )
        existing = writer.list_auto_providers()
        if not existing:
            log.info("No auto providers to re-validate")
            return
        log.info("Re-validating %d existing auto providers", len(existing))
        # Re-run collection + validation for existing sources
        tokens = collect_all(config)
        if tokens:
            results = validate_tokens(tokens, config)
            if results:
                write_to_ccswitch(results, config)

    def cleanup(max_failures: int = 3):
        cs_cfg = config.get("ccswitch", {})
        writer = CcSwitchWriter(
            db_path=cs_cfg.get("db_path", r"C:\Users\Administrator\.cc-switch\cc-switch.db"),
            auto_category=cs_cfg.get("auto_category", "auto_discovered"),
        )
        removed = writer.cleanup_expired(max_failures)
        log.info("Cleaned up %d expired providers", removed)

    run_daemon(collect_and_write, validate_existing, cleanup, config)


def cmd_validate(config: dict) -> None:
    """Re-validate existing auto providers."""
    cs_cfg = config.get("ccswitch", {})
    writer = CcSwitchWriter(
        db_path=cs_cfg.get("db_path", r"C:\Users\Administrator\.cc-switch\cc-switch.db"),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
    )
    existing = writer.list_auto_providers()
    log.info("Found %d existing auto providers", len(existing))
    for p in existing:
        log.info("  %s [%s] %s", p["id"], p["app_type"], p["name"])


def cmd_clean(config: dict) -> None:
    """Clean up expired providers."""
    cs_cfg = config.get("ccswitch", {})
    sched_cfg = config.get("scheduler", {})
    writer = CcSwitchWriter(
        db_path=cs_cfg.get("db_path", r"C:\Users\Administrator\.cc-switch\cc-switch.db"),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
    )
    max_f = sched_cfg.get("max_consecutive_failures", 3)
    removed = writer.cleanup_expired(max_f)
    log.info("Cleaned up %d expired providers", removed)


def cmd_list(config: dict) -> None:
    """List auto-discovered providers."""
    cs_cfg = config.get("ccswitch", {})
    writer = CcSwitchWriter(
        db_path=cs_cfg.get("db_path", r"C:\Users\Administrator\.cc-switch\cc-switch.db"),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
    )
    providers = writer.list_auto_providers()
    if not providers:
        print("No auto-discovered providers found.")
        return
    print(f"\n{'ID':<30} {'Type':<12} {'Name'}")
    print("-" * 80)
    for p in providers:
        print(f"{p['id']:<30} {p['app_type']:<12} {p['name']}")
    print(f"\nTotal: {len(providers)} providers")


# ── main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Free Token API Collector — collect, validate, write to cc-switch"
    )
    parser.add_argument("--daemon", action="store_true", help="Run as daemon with periodic refresh")
    parser.add_argument("--validate", action="store_true", help="Re-validate existing auto providers")
    parser.add_argument("--clean", action="store_true", help="Clean up expired providers")
    parser.add_argument("--list", action="store_true", help="List auto-discovered providers")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    args = parser.parse_args()

    setup_logging()
    config = load_config(args.config)

    if args.daemon:
        cmd_daemon(config)
    elif args.validate:
        cmd_validate(config)
    elif args.clean:
        cmd_clean(config)
    elif args.list:
        cmd_list(config)
    else:
        cmd_run(config)


if __name__ == "__main__":
    main()
