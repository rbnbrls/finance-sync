"""Validate and publish the synthetic connector certification matrix."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from finance_sync.services.connector_certification import (
    CertificationError,
    validate_certification,
)


def build_report(
    matrix: dict[str, Any], *, today: date | None = None
) -> dict[str, Any]:
    current = today or datetime.now(UTC).date()
    certifications = []
    for entry in matrix.get("connectors", []):
        if not isinstance(entry, dict):
            message = "connector_entry_invalid"
            raise CertificationError(message)
        certifications.append(
            validate_certification(
                matrix,
                str(entry.get("name", "")),
                str(entry.get("version", "")),
                today=current,
            )
        )
    return {
        "generated_at": current.isoformat(),
        "synthetic_data_only": True,
        "release_gate": "passed",
        "certifications": certifications,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path("config/connector-certification.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("connector-certification-report.json"),
    )
    args = parser.parse_args()
    try:
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        report = build_report(matrix)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (
        OSError,
        json.JSONDecodeError,
        CertificationError,
        TypeError,
        ValueError,
    ) as exc:
        sys.stderr.write(
            f"connector certification failed: {type(exc).__name__}: {exc}\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
