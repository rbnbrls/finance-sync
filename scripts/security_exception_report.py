"""Validate and publish the accepted-security-exception lifecycle report."""

# ruff: noqa: T201

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from scripts.check_trivyignore import parse_entries, validate

OWNER = os.environ.get("SECURITY_EXCEPTION_OWNER", "platform-security")
ISSUE = os.environ.get(
    "SECURITY_EXCEPTION_ISSUE",
    "https://github.com/rbnbrls/finance-sync/issues/233",
)


def build_report(path: Path, *, today: date | None = None) -> dict[str, object]:
    validate(path, today=today)
    entries = parse_entries(path)
    if not OWNER or not ISSUE.startswith("https://"):
        message = "security exception owner and issue link are required"
        raise ValueError(message)
    report_entries = [
        {
            "advisory": finding,
            "rationale": rationale,
            "owner": OWNER,
            "issue": ISSUE,
            "expiry": expiry,
        }
        for finding, expiry, rationale in entries
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(path),
        "entries": report_entries,
        "contains_secrets": False,
        "contains_financial_data": False,
    }


def main() -> int:
    source = Path(sys.argv[1] if len(sys.argv) > 1 else ".trivyignore")
    output = Path(
        sys.argv[2] if len(sys.argv) > 2 else "security-exceptions.json"
    )
    try:
        report = build_report(source)
    except (OSError, ValueError) as exc:
        print(f"security exception lifecycle failed: {exc}", file=sys.stderr)
        return 1
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
