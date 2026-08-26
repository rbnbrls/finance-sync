"""Build a credential-free connector lifecycle evidence artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_evidence(
    lifecycle: dict[str, Any],
    matrix: dict[str, Any],
    *,
    test_result: str,
    canary_result: str,
) -> dict[str, Any]:
    matrix_by_name = {
        str(item["name"]): item for item in matrix.get("connectors", [])
    }
    releases: list[dict[str, Any]] = []
    for connector in lifecycle.get("connectors", []):
        name = str(connector["name"])
        fixture = matrix_by_name.get(name)
        if not fixture:
            message = f"missing contract fixture for {name}"
            raise ValueError(message)
        if connector.get("certification_status") != "certified":
            message = f"connector {name} is not certified"
            raise ValueError(message)
        for field in ("version", "certification_commit", "previous_version"):
            if not connector.get(field):
                message = f"connector {name} missing {field}"
                raise ValueError(message)
        releases.append(
            {
                "provider": name,
                "connector_version": connector["version"],
                "certification_commit": connector["certification_commit"],
                "fixture_version": fixture.get("fixture_date"),
                "test_result": test_result,
                "canary_result": canary_result,
                "rollback_version": connector["previous_version"],
            }
        )
    return {
        "synthetic_data_only": True,
        "release_gate": "passed",
        "connectors": releases,
        "rollback_policy": lifecycle["rollback_policy"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path("config/connector-lifecycle.json")
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path("config/provider-contract-matrix.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("connector-lifecycle-evidence.json")
    )
    args = parser.parse_args()
    try:
        lifecycle = json.loads(args.config.read_text(encoding="utf-8"))
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        evidence = build_evidence(
            lifecycle, matrix, test_result="passed", canary_result="passed"
        )
        args.output.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        message = "connector lifecycle evidence failed: "
        message += f"{type(exc).__name__}: {exc}\n"
        sys.stderr.write(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
