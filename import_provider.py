"""Import a single provider into cc-switch with validation.

Usage example:
    python import_provider.py ^
        --base-url https://token-plan-cn.xiaomimimo.com/v1 ^
        --api-key YOUR_KEY ^
        --source manual:demo
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from db_writer import CcSwitchWriter
from logger import setup_logging
from main import load_config, resolve_ccswitch_db_path
from sources.base import DiscoveredToken
from validator import TokenValidator

log = logging.getLogger("import_provider")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a single provider and import it into cc-switch"
    )
    parser.add_argument("--base-url", required=True, help="Provider base URL, e.g. https://example.com/v1")
    parser.add_argument("--api-key", required=True, help="Provider API key")
    parser.add_argument(
        "--source",
        default="manual:import",
        help="Source label stored in provider metadata",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Preferred model to validate. Can be passed multiple times.",
    )
    parser.add_argument(
        "--skip-discover-models",
        action="store_true",
        help="Skip GET /models discovery and only use provided/default models",
    )
    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Only validate and print the sample reply; do not write to cc-switch",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Config file path",
    )
    return parser.parse_args()


async def discover_and_validate(
    token: DiscoveredToken,
    config: dict,
    skip_discover_models: bool,
) -> tuple[list[str], list]:
    network = config.get("network", {})
    validator_cfg = dict(config.get("validator", {}))
    if skip_discover_models:
        validator_cfg["discover_models"] = False
    validator = TokenValidator(network, validator_cfg)

    models = list(token.raw_models)
    if not skip_discover_models:
        discovered = await validator.discover_models(token)
        if discovered:
            models = discovered
            token.raw_models = discovered[:10]

    if not token.raw_models:
        token.raw_models = [validator_cfg.get("test_model_openai", "gpt-4o")]

    results = await validator.validate_all([token])
    return models, results


def import_single_provider(
    *,
    base_url: str,
    api_key: str,
    source: str,
    models: list[str] | None = None,
    skip_discover_models: bool = False,
    test_only: bool = False,
    config_path: str = "config.yaml",
) -> dict[str, Any]:
    """Validate a single provider and optionally write it into cc-switch."""
    config = load_config(config_path)
    db_path = resolve_ccswitch_db_path(config)
    token = DiscoveredToken(
        source=source,
        base_url=base_url,
        api_key=api_key,
        raw_models=(models or [])[:10],
    )

    discovered_models, results = asyncio.run(
        discover_and_validate(token, config, skip_discover_models)
    )
    if not results:
        raise RuntimeError("Validation produced no results.")

    result = results[0]
    if not result.is_healthy:
        raise RuntimeError(
            "Provider validation failed: "
            f"status={result.http_status} protocol={result.protocol.value} "
            f"error={result.error or 'unknown_error'}"
        )

    base_result = {
        "db_path": db_path,
        "provider_id": result.provider_id,
        "app_type": result.app_type,
        "protocol": result.protocol.value,
        "validated_model": (result.discovered_models or token.raw_models or [""])[0],
        "discovered_models": discovered_models[:10],
        "sample_reply": result.sample_reply,
        "written": False,
        "summary": {
            "healthy_written": 0,
            "failure_updates": 0,
            "skipped_manual": 0,
            "skipped_unhealthy_new": 0,
        },
    }
    if test_only:
        return base_result

    writer = CcSwitchWriter(
        db_path=db_path,
        auto_category=config.get("ccswitch", {}).get("auto_category", "auto_discovered"),
        never_overwrite=config.get("ccswitch", {}).get("never_overwrite", True),
    )
    summary = writer.sync_results([result])

    return {
        **base_result,
        "written": True,
        "summary": summary,
    }


def print_import_result(import_result: dict[str, Any]) -> None:
    """Print a stable, easy-to-parse summary."""
    print(f"db_path={import_result['db_path']}")
    print(f"provider_id={import_result['provider_id']}")
    print(f"app_type={import_result['app_type']}")
    print(f"protocol={import_result['protocol']}")
    print(f"validated_model={import_result['validated_model']}")
    print(f"discovered_models={','.join(import_result['discovered_models'])}")
    print(f"sample_reply={import_result['sample_reply']}")
    print(f"written={'yes' if import_result.get('written') else 'no'}")
    summary = import_result["summary"]
    print(
        "summary="
        f"healthy_written={summary['healthy_written']},"
        f"failure_updates={summary['failure_updates']},"
        f"skipped_manual={summary['skipped_manual']},"
        f"skipped_unhealthy_new={summary['skipped_unhealthy_new']}"
    )


def main() -> None:
    args = parse_args()
    setup_logging()
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


if __name__ == "__main__":
    main()
