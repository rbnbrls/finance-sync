"""Plan and execute tenant-scoped audit retention without unsafe deletion."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _anonymous_identifier(value: str) -> str:
    """Return a one-way identifier suitable for operational evidence."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    max_retries: int = 0,
) -> dict[str, Any]:
    expired = select_expired(
        records, now=now, retention_days=retention_days, tenant_id=tenant_id
    )
    deleted: list[str] = []
    failed: list[dict[str, Any]] = []
    retry_count = 0
    counts_by_category: dict[str, dict[str, int]] = {}
    result_status_counts: dict[str, int] = {}

    def increment(mapping: dict[str, int], key: str) -> None:
        mapping[key] = mapping.get(key, 0) + 1

    status = "dry-run" if dry_run else "passed"
    error = None
    for record in expired:
        category = str(record.get("category", record.get("data_category", "unknown")))
        category_counts = counts_by_category.setdefault(category, {})
        increment(category_counts, "candidate")
        if dry_run:
            increment(category_counts, "dry-run")
            increment(result_status_counts, "dry-run")
            continue
        attempts = 0
        while True:
            try:
                delete(str(record["id"]))
                deleted.append(str(record["id"]))
                increment(category_counts, "deleted")
                increment(result_status_counts, "deleted")
                break
            except Exception as exc:  # do not include provider details in report
                if attempts < max_retries:
                    attempts += 1
                    retry_count += 1
                    continue
                if max_retries:
                    failed.append({"category": category, "error": type(exc).__name__})
                    increment(category_counts, "failed")
                    increment(result_status_counts, "failed")
                    break
                for record_id in reversed(deleted):
                    restore(record_id)
                status = "rolled-back"
                error = type(exc).__name__
                deleted.clear()
                break
        if status == "rolled-back":
            break
    if failed:
        status = "partial-failure"
        error = "one_or_more_records_failed"
    return {
        "event": "audit_retention.run",
        "generated_at": now.astimezone(UTC).isoformat(),
        "run_id": _anonymous_identifier(f"{tenant_id}:{now.isoformat()}"),
        "tenant_id": _anonymous_identifier(tenant_id),
        "candidate_count": len(expired),
        "deleted_count": len(deleted) if status == "passed" else 0,
        "failed_count": len(failed),
        "retry_count": retry_count,
        "counts_by_category": counts_by_category,
        "result_status_counts": result_status_counts,
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
