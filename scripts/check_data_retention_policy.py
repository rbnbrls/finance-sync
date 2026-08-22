"""Validate the data-retention and privacy policy contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_CATEGORIES = {
    "credentials",
    "audit_data",
    "outbox_payloads",
    "logs",
    "financial_facts",
    "provider_payloads",
}


def validate(policy: dict[str, Any]) -> None:
    categories = policy.get("categories", [])
    by_name = {str(item.get("name")): item for item in categories}
    missing = REQUIRED_CATEGORIES - set(by_name)
    if missing:
        message = "missing retention categories: " + ", ".join(sorted(missing))
        raise ValueError(message)
    if int(policy.get("review_cadence_days", 0)) > 90:
        message = "privacy policy review cadence exceeds 90 days"
        raise ValueError(message)
    for name, item in by_name.items():
        if not item.get("locations") or not item.get("deletion"):
            message = f"incomplete retention policy: {name}"
            raise ValueError(message)
        if not item.get("tenant_scoped"):
            message = f"retention category is not tenant-scoped: {name}"
            raise ValueError(message)
        if name != "financial_facts" and not item.get("redacted"):
            message = f"retention category is not redacted: {name}"
            raise ValueError(message)


def main() -> int:
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("config/data-retention-policy.json")
    )
    try:
        validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"data retention policy failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
