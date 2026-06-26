"""Free Token API Collector — main entry point.

Usage:
    python main.py              # 一次性采集 + 验证 + 写入 cc-switch
    python main.py --daemon     # 后台守护模式（定时刷新）
    python main.py --validate   # 仅验证已有 auto provider
    python main.py --clean      # 清理过期/失败 token
    python main.py --purge      # 遍历 cc-switch 验证并删除失效 API Key
    python main.py --list       # 列出当前自动发现的 provider
    python main.py --import --base-url URL --api-key KEY
    python main.py --import-file providers.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from logger import setup_logging
from db_writer import CcSwitchWriter
from validator import TokenValidator
from sources import GitHubReadmeSource, GitHubSearchSource, WebAggregatorSource, ForumSource, DiscoveredToken

log = logging.getLogger("collector")

# ── helpers ──────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    p = Path(__file__).parent / path
    if not p.exists():
        log.error("Config not found: %s", p)
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_ccswitch_db_path(config: dict) -> str:
    """Resolve the cc-switch DB path, falling back to the current user's profile."""
    cs_cfg = config.get("ccswitch", {})
    configured = cs_cfg.get("db_path", "")
    candidates: list[Path] = []

    if configured:
        candidates.append(Path(configured))

    home = Path.home()
    candidates.append(home / ".cc-switch" / "cc-switch.db")

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(Path(user_profile) / ".cc-switch" / "cc-switch.db")

    for candidate in candidates:
        if candidate.exists():
            if configured and Path(configured) != candidate:
                log.info("Using detected cc-switch DB path: %s", candidate)
            return str(candidate)

    return configured or str(home / ".cc-switch" / "cc-switch.db")


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
            elif src_type == "forum":
                src = ForumSource(src_cfg, network)
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
    """Validate tokens and return full health results."""
    network = config.get("network", {})
    val_cfg = config.get("validator", {})
    validator = TokenValidator(network, val_cfg)

    log.info("━━━ Validating %d tokens ━━━", len(tokens))
    results = asyncio.run(validator.validate_all(tokens))
    healthy_count = sum(1 for result in results if result.is_healthy)
    log.info("Healthy tokens: %d / %d", healthy_count, len(tokens))
    return results


def write_to_ccswitch(results: list, config: dict) -> dict[str, int]:
    """Write validation results to cc-switch DB and health tables."""
    cs_cfg = config.get("ccswitch", {})
    writer = CcSwitchWriter(
        db_path=resolve_ccswitch_db_path(config),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
        never_overwrite=cs_cfg.get("never_overwrite", True),
    )
    log.info("━━━ Writing to cc-switch ━━━")
    summary = writer.sync_results(results)
    log.info(
        "Healthy writes=%d, failure updates=%d, skipped manual=%d, skipped unhealthy new=%d",
        summary["healthy_written"],
        summary["failure_updates"],
        summary["skipped_manual"],
        summary["skipped_unhealthy_new"],
    )
    return summary


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
        log.warning("No validation results produced.")
        return

    summary = write_to_ccswitch(results, config)
    elapsed = time.time() - t0
    log.info(
        "Done in %.1fs — healthy=%d, failures_recorded=%d",
        elapsed,
        summary["healthy_written"],
        summary["failure_updates"],
    )


def cmd_daemon(config: dict) -> None:
    """Daemon mode with periodic refresh."""
    from scheduler import run_daemon

    def collect_and_write():
        tokens = collect_all(config)
        if tokens:
            results = validate_tokens(tokens, config)
            write_to_ccswitch(results, config)

    def validate_existing():
        cs_cfg = config.get("ccswitch", {})
        writer = CcSwitchWriter(
            db_path=resolve_ccswitch_db_path(config),
            auto_category=cs_cfg.get("auto_category", "auto_discovered"),
        )
        tokens = writer.list_managed_provider_tokens()
        if not tokens:
            log.info("No managed providers to re-validate")
            return
        log.info("Re-validating %d managed providers", len(tokens))
        results = validate_tokens(tokens, config)
        write_to_ccswitch(results, config)

    def cleanup(max_failures: int = 3):
        cs_cfg = config.get("ccswitch", {})
        writer = CcSwitchWriter(
            db_path=resolve_ccswitch_db_path(config),
            auto_category=cs_cfg.get("auto_category", "auto_discovered"),
        )
        removed = writer.cleanup_expired(max_failures)
        log.info("Cleaned up %d expired providers", removed)

    run_daemon(collect_and_write, validate_existing, cleanup, config)


def cmd_validate(config: dict) -> None:
    """Re-validate existing auto providers."""
    cs_cfg = config.get("ccswitch", {})
    writer = CcSwitchWriter(
        db_path=resolve_ccswitch_db_path(config),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
    )
    tokens = writer.list_managed_provider_tokens()
    log.info("Found %d managed providers for validation", len(tokens))
    if not tokens:
        return

    results = validate_tokens(tokens, config)
    summary = write_to_ccswitch(results, config)
    log.info(
        "Re-validation finished — healthy=%d, failures_recorded=%d",
        summary["healthy_written"],
        summary["failure_updates"],
    )


def cmd_clean(config: dict) -> None:
    """Clean up expired providers."""
    cs_cfg = config.get("ccswitch", {})
    sched_cfg = config.get("scheduler", {})
    writer = CcSwitchWriter(
        db_path=resolve_ccswitch_db_path(config),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
    )
    max_f = sched_cfg.get("max_consecutive_failures", 3)
    removed = writer.cleanup_expired(max_f)
    log.info("Cleaned up %d expired providers", removed)


def cmd_purge(config: dict, args: argparse.Namespace) -> None:
    """Traverse cc-switch providers, validate, and remove expired API keys."""
    cs_cfg = config.get("ccswitch", {})
    writer = CcSwitchWriter(
        db_path=resolve_ccswitch_db_path(config),
        auto_category=cs_cfg.get("auto_category", "auto_discovered"),
        never_overwrite=cs_cfg.get("never_overwrite", True),
    )
    db_path = resolve_ccswitch_db_path(config)
    dry_run = args.dry_run

    if dry_run:
        log.info("━━━ Dry-run purge (no database changes) ━━━")
    else:
        log.info("━━━ Purging expired API keys from cc-switch ━━━")
    log.info("Database: %s", db_path)

    already_expired = writer.list_expired_providers()
    if already_expired:
        log.info("Found %d providers already marked expired", len(already_expired))
    removed_expired = writer.purge_already_expired(dry_run=dry_run)

    tokens = writer.list_managed_provider_tokens()
    log.info("Found %d auto-managed providers to validate", len(tokens))

    purge_summary = {
        "validated": 0,
        "kept_healthy": 0,
        "removed_unhealthy": 0,
        "skipped_manual": 0,
        "removed_already_expired": removed_expired,
    }
    if tokens:
        results = validate_tokens(tokens, config)
        purge_summary = writer.purge_invalid_providers(results, dry_run=dry_run)
        purge_summary["removed_already_expired"] = removed_expired
        if not dry_run:
            healthy_results = [result for result in results if result.is_healthy]
            if healthy_results:
                sync_summary = writer.sync_results(healthy_results)
                log.info(
                    "Reconciled healthy providers — updated=%d, failures_recorded=%d",
                    sync_summary["healthy_written"],
                    sync_summary["failure_updates"],
                )
    elif not already_expired:
        log.info("No auto-managed providers found in cc-switch.")
        return

    log.info(
        "Purge finished — validated=%d, kept=%d, removed_unhealthy=%d, "
        "removed_already_expired=%d, skipped_manual=%d%s",
        purge_summary["validated"],
        purge_summary["kept_healthy"],
        purge_summary["removed_unhealthy"],
        purge_summary["removed_already_expired"],
        purge_summary["skipped_manual"],
        " (dry-run)" if dry_run else "",
    )


def cmd_list(config: dict) -> None:
    """List auto-discovered providers."""
    cs_cfg = config.get("ccswitch", {})
    writer = CcSwitchWriter(
        db_path=resolve_ccswitch_db_path(config),
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


def cmd_import_provider(config: dict, args: argparse.Namespace) -> None:
    """Import a single provider after validation."""
    from import_provider import import_single_provider, print_import_result

    try:
        import_result = import_single_provider(
            base_url=args.base_url,
            api_key=args.api_key,
            source=args.source,
            models=args.model,
            skip_discover_models=args.skip_discover_models,
            test_only=args.test_only,
            config_path=args.config,
        )
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)

    print_import_result(import_result)


def cmd_import_file(config: dict, args: argparse.Namespace) -> None:
    """Batch import providers from a YAML file."""
    from import_providers import load_provider_specs
    from import_provider import import_single_provider

    try:
        providers = load_provider_specs(args.import_file)
    except Exception as e:
        log.error("Failed to load provider file: %s", e)
        sys.exit(1)

    success_count = 0
    failed_count = 0

    for index, spec in enumerate(providers, start=1):
        print(f"\n[{index}/{len(providers)}] source={spec['source']} base_url={spec['base_url']}")
        try:
            result = import_single_provider(
                base_url=spec["base_url"],
                api_key=spec["api_key"],
                source=spec["source"],
                models=spec["models"],
                skip_discover_models=spec["skip_discover_models"],
                test_only=args.test_only,
                config_path=args.config,
            )
            print(
                "result="
                f"provider_id={result['provider_id']},"
                f"app_type={result['app_type']},"
                f"validated_model={result['validated_model']},"
                f"sample_reply={result['sample_reply']},"
                f"written={'yes' if result.get('written') else 'no'}"
            )
            success_count += 1
        except Exception as e:
            failed_count += 1
            log.error("Import failed for %s: %s", spec["source"], e)
            if not args.continue_on_error:
                break

    print(
        "\nsummary="
        f"total={len(providers)},"
        f"success={success_count},"
        f"failed={failed_count}"
    )
    if failed_count:
        sys.exit(1)


# ── main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Free Token API Collector — collect, validate, write to cc-switch"
    )
    parser.add_argument("--daemon", action="store_true", help="Run as daemon with periodic refresh")
    parser.add_argument("--validate", action="store_true", help="Re-validate existing auto providers")
    parser.add_argument("--clean", action="store_true", help="Clean up expired providers")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Validate all auto-managed providers in cc-switch and delete invalid API keys",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --purge: preview removals without changing the database",
    )
    parser.add_argument("--list", action="store_true", help="List auto-discovered providers")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Import a single provider")
    parser.add_argument("--import-file", metavar="FILE", help="Batch import providers from YAML")
    parser.add_argument("--base-url", help="Provider base URL (with --import)")
    parser.add_argument("--api-key", help="Provider API key (with --import)")
    parser.add_argument("--source", default="manual:import", help="Source label for manual import")
    parser.add_argument("--model", action="append", default=[], help="Preferred model(s) for import")
    parser.add_argument("--skip-discover-models", action="store_true", help="Skip GET /models during import")
    parser.add_argument(
        "--test-only",
        action="store_true",
        help="With --import: validate and show sample reply without writing to cc-switch",
    )
    parser.add_argument("--continue-on-error", action="store_true", help="Continue batch import on failure")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    args = parser.parse_args()

    setup_logging()
    config = load_config(args.config)

    if args.import_file:
        cmd_import_file(config, args)
    elif args.do_import:
        if not args.base_url or not args.api_key:
            parser.error("--import requires --base-url and --api-key")
        cmd_import_provider(config, args)
    elif args.daemon:
        cmd_daemon(config)
    elif args.validate:
        cmd_validate(config)
    elif args.clean:
        cmd_clean(config)
    elif args.purge:
        cmd_purge(config, args)
    elif args.list:
        cmd_list(config)
    else:
        cmd_run(config)


if __name__ == "__main__":
    main()
