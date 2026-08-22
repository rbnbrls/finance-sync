"""Evaluate connector version lifecycle and safe diagnostics."""

# ruff: noqa: E501, DTZ011

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def evaluate(
    lifecycle: dict[str, Any], *, today: date, fixture_version: str, enabled: bool = True
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for connector in lifecycle.get("connectors", []):
        name = str(connector["name"])
        status = "disabled" if not enabled else "healthy"
        reason = "feature_flag_disabled" if not enabled else "compatible"
        if not VERSION.match(str(connector["version"])):
            status, reason = "incompatible", "invalid_version"
        elif fixture_version < str(connector["minimum_fixture_version"]):
            status, reason = "incompatible", "fixture_too_old"
        elif connector.get("deprecation_date") and today >= date.fromisoformat(str(connector["deprecation_date"])):
            status, reason = "deprecated", "deprecation_date_reached"
        diagnostics.append(
            {
                "connector": name,
                "version": connector["version"],
                "status": status,
                "reason": reason,
                "capabilities": connector["capabilities"],
                "removal_date": connector["removal_date"],
                "rollback_version": connector["previous_version"],
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
