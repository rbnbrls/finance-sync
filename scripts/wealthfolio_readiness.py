#!/usr/bin/env python3
# ruff: noqa: T201
"""Read-only readiness check for a live Wealthfolio delivery deployment.

The command intentionally reports only booleans and safe metadata.  It never
prints secret values, response bodies, cookies, account data, or financial
amounts.  Use it inside the API/worker deployment before a controlled push.

    uv run python scripts/wealthfolio_readiness.py
    uv run python scripts/wealthfolio_readiness.py --skip-network
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime
from typing import Any

from finance_sync.exporter.wealthfolio.client import (
    WealthfolioClient,
    WealthfolioClientConfig,
)


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()[:40]
    except (OSError, subprocess.SubprocessError):
        return None


async def _network_check(url: str, password: str) -> dict[str, Any]:
    client = WealthfolioClient(
        config=WealthfolioClientConfig(
            base_url=url,
            password=password,
            request_timeout=30.0,
        )
    )
    try:
        status = await client.check_auth_status()
        await client.authenticate()
        return {
            "reachable": True,
            "auth_status_ok": True,
            "requires_password": bool(status.get("requiresPassword")),
            "authenticated": client.is_authenticated,
        }
    except Exception as exc:
        return {
            "reachable": False,
            "auth_status_ok": False,
            "authenticated": False,
            "error_type": type(exc).__name__,
        }
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help=(
            "Only validate deployment configuration; do not contact "
            "Wealthfolio."
        ),
    )
    args = parser.parse_args()

    url = os.getenv("WEALTHFOLIO_SERVER_URL", "").strip()
    password = os.getenv("WEALTHFOLIO_PASSWORD", "")
    required = {
        "WEALTHFOLIO_SERVER_URL": bool(url),
        "WEALTHFOLIO_PASSWORD": bool(password),
        "WORKER_JOB_EXPORT_ENABLED": os.getenv(
            "WORKER_JOB_EXPORT_ENABLED", ""
        ).lower()
        == "true",
    }
    report: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "commit": _git_commit(),
        "environment": os.getenv("APP_ENVIRONMENT", "unknown"),
        "worker_job_export_enabled": required["WORKER_JOB_EXPORT_ENABLED"],
        "required_configuration": required,
        "connector_configuration": {
            "store": "existing connection API",
            "credentials_in_environment": bool(
                os.getenv("BUNQ_API_KEY")
                or os.getenv("TRADING212_API_KEY")
                or os.getenv("TRADING212_API_SECRET")
            ),
            "verification": "run connector config test endpoint",
        },
        "network": {"skipped": args.skip_network},
    }
    if not args.skip_network and url and password:
        report["network"] = asyncio.run(_network_check(url, password))

    print(json.dumps(report, sort_keys=True))
    config_ok = all(required.values())
    network_ok = args.skip_network or report["network"].get(
        "authenticated", False
    )
    raise SystemExit(0 if config_ok and network_ok else 1)


if __name__ == "__main__":
    main()
