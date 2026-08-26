"""Evaluate connector version lifecycle and safe diagnostics."""

# ruff: noqa: E501, DTZ011

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from finance_sync.services.connector_compatibility import evaluate_connector


def evaluate(
    lifecycle: dict[str, Any], *, today: date, fixture_version: str, enabled: bool = True
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for connector in lifecycle.get("connectors", []):
        name = str(connector["name"])
        metadata = {
            "name": name,
            "provider_key": name,
            "plugin_version": connector.get("version"),
            "supported_resources": connector.get("capabilities", []),
        }
        compatibility = evaluate_connector(
            lifecycle,
            metadata,
            today=today,
            fixture_version=fixture_version,
            enabled=enabled,
        )
        diagnostics.append(
            {
                "connector": name,
                "version": compatibility.current_version,
                "status": (
                    "healthy"
                    if compatibility.status == "compatible"
                    else compatibility.status
                ),
                "reason": compatibility.reason,
                "capabilities": connector["capabilities"],
                "removal_date": (
                    compatibility.removal_date.isoformat()
                    if compatibility.removal_date
                    else None
                ),
                "rollback_version": compatibility.previous_version,
                "certification_status": compatibility.certification_status,
                "certified_at": (
                    compatibility.certified_at.isoformat()
                    if compatibility.certified_at
                    else None
                ),
                "certification_commit": compatibility.certification_commit,
                "migration_required": compatibility.migration_required,
                "warnings": compatibility.warnings,
                "credentials_included": False,
            }
        )
    return diagnostics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/connector-lifecycle.json"))
    parser.add_argument("--artifact", type=Path, default=Path("connector-lifecycle-report.json"))
    args = parser.parse_args()
    try:
        lifecycle = json.loads(args.config.read_text(encoding="utf-8"))
        report = {
            "generated_at": date.today().isoformat(),
            "diagnostics": evaluate(lifecycle, today=date.today(), fixture_version="2026-01-15"),
            "rollback_policy": lifecycle["rollback_policy"],
            "synthetic_data_only": True,
        }
        args.artifact.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        sys.stderr.write(f"connector lifecycle failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
