"""Validate audit-trail coverage and a synthetic incident investigation."""

# ruff: noqa: E501, EM101

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_OBJECTS = {"credential", "sync_configuration", "security_resolution", "export"}
REQUIRED_FIELDS = {"actor", "timestamp", "tenant", "object_type", "action", "redacted_diff"}


def validate_policy(policy: dict[str, Any]) -> None:
    objects = {str(item["object_type"]) for item in policy.get("events", [])}
    if missing := REQUIRED_OBJECTS - objects:
        raise ValueError("missing audit objects: " + ", ".join(sorted(missing)))
    if set(policy.get("required_fields", [])) != REQUIRED_FIELDS:
        raise ValueError("audit required fields are incomplete")
    if not policy.get("read_only") or not policy.get("read_roles"):
        raise ValueError("audit access must be read-only and role-scoped")
    if int(policy.get("retention_days", 0)) < 365:
        raise ValueError("audit retention is too short")


def validate_incident(example: dict[str, Any], policy: dict[str, Any]) -> None:
    forbidden = {str(item).lower() for item in policy.get("forbidden_fields", [])}
    for record in example.get("records", []):
        if set(record) != REQUIRED_FIELDS:
            raise ValueError("incident record does not contain the complete audit shape")
        if not all(record.get(field) for field in REQUIRED_FIELDS):
            raise ValueError("incident record contains an empty required field")
        if forbidden & {str(key).lower() for key in record["redacted_diff"]}:
            raise ValueError("sensitive field found in redacted diff")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=Path("config/audit-trail-policy.json"))
    parser.add_argument("--example", type=Path, default=Path("config/incident-audit-example.json"))
    args = parser.parse_args()
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        validate_policy(policy)
        validate_incident(json.loads(args.example.read_text(encoding="utf-8")), policy)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"audit trail completeness failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
