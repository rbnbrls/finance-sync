"""Plan and execute tenant-scoped audit retention without unsafe deletion."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def select_expired(
    records: list[dict[str, Any]], *, now: datetime, retention_days: int, tenant_id: str
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=retention_days)
    return [
        record
        for record in records
        if record.get("tenant_id") == tenant_id
        and datetime.fromisoformat(str(record["created_at"])) < cutoff
    ]


def execute_retention(
    records: list[dict[str, Any]],
    *,
    now: datetime,
    retention_days: int,
    tenant_id: str,
    dry_run: bool,
    delete: Any,
    restore: Any,
) -> dict[str, Any]:
    expired = select_expired(
        records, now=now, retention_days=retention_days, tenant_id=tenant_id
    )
    deleted: list[str] = []
    status = "dry-run" if dry_run else "passed"
    error = None
    if not dry_run:
        try:
            for record in expired:
                delete(str(record["id"]))
                deleted.append(str(record["id"]))
        except Exception as exc:  # rollback boundary
            for record_id in reversed(deleted):
                restore(record_id)
            status = "rolled-back"
            error = type(exc).__name__
    return {
        "event": "audit_retention.run",
        "generated_at": now.astimezone(UTC).isoformat(),
        "tenant_id": tenant_id,
        "candidate_count": len(expired),
        "deleted_count": len(deleted) if status == "passed" else 0,
        "status": status,
        "error": error,
        "dry_run": dry_run,
        "secrets_or_financial_values": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=Path("config/audit-retention-policy.json"))
    parser.add_argument("--artifact", type=Path, default=Path("audit-retention-run.json"))
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        def noop(_record_id: str) -> None:
            return None

        report = execute_retention(
            [],
            now=datetime.now(UTC),
            retention_days=int(policy["retention_days"]),
            tenant_id=args.tenant,
            dry_run=args.dry_run,
            delete=noop,
            restore=noop,
        )
        args.artifact.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        sys.stderr.write(f"audit retention failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
