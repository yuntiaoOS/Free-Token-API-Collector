"""Batch import providers from a YAML file.

Usage example:
    python import_providers.py --file providers.example.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys

import yaml

from import_provider import import_single_provider
from logger import setup_logging

log = logging.getLogger("import_providers")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch validate and import providers into cc-switch"
    )
    parser.add_argument(
        "--file",
        required=True,
        help="YAML file containing providers to import",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Project config file path",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue importing remaining providers when one item fails",
    )
    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Only validate and print sample replies; do not write to cc-switch",
    )
    return parser.parse_args()


def load_provider_specs(file_path: str) -> list[dict]:
    with open(file_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}

    providers = payload.get("providers", payload)
    if not isinstance(providers, list):
        raise ValueError("Provider file must be a list or contain a top-level 'providers' list")

    normalized: list[dict] = []
    for i, item in enumerate(providers, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Provider item #{i} must be an object")
        base_url = item.get("base_url")
        api_key = item.get("api_key")
        if not base_url or not api_key:
            raise ValueError(f"Provider item #{i} requires 'base_url' and 'api_key'")
        normalized.append(
            {
                "base_url": base_url,
                "api_key": api_key,
                "source": item.get("source", f"manual:batch-{i}"),
                "models": item.get("models", []) or [],
                "skip_discover_models": bool(item.get("skip_discover_models", False)),
            }
        )
    return normalized


def main() -> None:
    args = parse_args()
    setup_logging()

    try:
        providers = load_provider_specs(args.file)
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


if __name__ == "__main__":
    main()
