"""Validate the synthetic provider compatibility matrix."""

# ruff: noqa: E501, EM101, EM102

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = {
    "accounts": {"external_id", "currency", "status"},
    "transactions": {"external_id", "account_id", "amount", "status"},
    "holdings": {"external_id", "security_id", "quantity", "currency"},
}


def validate_matrix(matrix: dict[str, Any]) -> None:
    if not matrix.get("synthetic_data_only"):
        raise ValueError("provider matrix must use synthetic data")
    for connector in matrix.get("connectors", []):
        name = str(connector.get("name"))
        if not connector.get("version"):
            raise ValueError(f"{name}: missing connector version")
        date.fromisoformat(str(connector.get("fixture_date")))
        capabilities = set(connector.get("capabilities", []))
        fixtures = connector.get("fixtures", {})
        if capabilities != set(fixtures):
            raise ValueError(f"{name}: capability/fixture mismatch")
        for resource in capabilities:
            fields = set(fixtures[resource].get("fields", {}))
            missing = REQUIRED_FIELDS.get(resource, set()) - fields
            if missing:
                raise ValueError(f"{name}/{resource}: missing fields {sorted(missing)}")
            for field, kind in fixtures[resource]["fields"].items():
                if kind.startswith("enum:"):
                    options = [
                        option
                        for option in kind.removeprefix("enum:").split("|")
                        if option
                    ]
                    if not options:
                        raise ValueError(f"{name}/{resource}/{field}: empty enum")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=Path("config/provider-contract-matrix.json"))
    parser.add_argument("--artifact", type=Path, default=Path("provider-contract-report.json"))
    args = parser.parse_args()
    try:
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        validate_matrix(matrix)
        report = {
            "synthetic_data_only": True,
            "connectors": [
                {"name": item["name"], "version": item["version"], "fixture_date": item["fixture_date"], "capabilities": item["capabilities"]}
                for item in matrix["connectors"]
            ],
            "status": "compatible",
        }
        args.artifact.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"provider contract refresh failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
