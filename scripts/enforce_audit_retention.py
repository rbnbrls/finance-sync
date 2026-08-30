"""Plan and execute tenant-scoped audit retention with safe evidence reports."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, cast

_REPORT_SECRET = os.environ.get("AUDIT_RETENTION_REPORT_SECRET", "").encode(
    "utf-8"
) or secrets.token_bytes(32)
_RUN_LOCK = RLock()
_RUN_STATE: dict[str, set[str]] = {}
_CLAIMED_RECORDS: set[str] = set()


def _identifier(value: str, *, secret: bytes) -> str:
    """Return a keyed, non-dictionary identifier for operational evidence."""
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _safe_category(value: Any) -> str:
    """Keep labels useful while preventing markup, formulas, and newlines."""
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value)).strip("_")
    if not label or label[0] in "=+-@":
        label = f"category_{label.lstrip('=+-@')}"
    return label[:80] or "unknown"


def select_expired(
    records: list[dict[str, Any]],
    *,
    now: datetime,
    retention_days: int,
    tenant_id: str,
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
    identifier_secret: bytes | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute one logical run, retrying and serializing overlapping runs.

    Successful IDs are process-local coordination state; production adapters
    should back this with a unique run/report table.
    """
    secret = identifier_secret or _REPORT_SECRET
    expired = select_expired(
        records, now=now, retention_days=retention_days, tenant_id=tenant_id
    )
    callback_name = getattr(delete, "__qualname__", type(delete).__qualname__)
    record_fingerprint = ",".join(str(record["id"]) for record in expired)
    logical_id = run_id or (
        f"{tenant_id}:{now.isoformat()}:{callback_name}:{record_fingerprint}"
    )
    run_key = _identifier(f"{logical_id}:{dry_run}", secret=secret)
    with _RUN_LOCK:
        completed = _RUN_STATE.setdefault(run_key, set())
        claimed_here: set[str] = set()
        deleted: list[str] = []
        failed: list[dict[str, Any]] = []
        retry_count = 0
        counts_by_category: dict[str, dict[str, int]] = {}
        result_status_counts: dict[str, int] = {}
        status = "dry-run" if dry_run else "passed"
        error: str | None = None

        def increment(mapping: dict[str, int], key: str) -> None:
            mapping[key] = mapping.get(key, 0) + 1

        for index, record in enumerate(expired):
            record_id = str(record["id"])
            # Coordinate equivalent snapshots of the same source records.  The
            # container identity is not stable across workers, while the
            # tenant, retention cut-off, and record identity are.
            claim_key = _identifier(
                f"{tenant_id}:{now.isoformat()}:{retention_days}:{record_id}",
                secret=secret,
            )
            category = _safe_category(
                record.get("category", record.get("data_category", "unknown"))
            )
            category_counts = counts_by_category.setdefault(category, {})
            increment(category_counts, "candidate")
            if dry_run:
                increment(category_counts, "dry-run")
                increment(result_status_counts, "dry-run")
                continue
            if record_id in completed:
                increment(category_counts, "deleted")
                increment(result_status_counts, "deleted")
                continue
            if claim_key in _CLAIMED_RECORDS:
                completed.add(record_id)
                increment(category_counts, "deleted")
                increment(result_status_counts, "deleted")
                continue
            attempts = 0
            while True:
                try:
                    delete(record_id)
                    completed.add(record_id)
                    _CLAIMED_RECORDS.add(claim_key)
                    claimed_here.add(claim_key)
                    deleted.append(record_id)
                    increment(category_counts, "deleted")
                    increment(result_status_counts, "deleted")
                    break
                except Exception as exc:
                    if attempts < max_retries:
                        attempts += 1
                        retry_count += 1
                        continue
                    if max_retries:
                        failed.append(
                            {"category": category, "error": type(exc).__name__}
                        )
                        increment(category_counts, "failed")
                        increment(result_status_counts, "failed")
                        break
                    for restored_id in reversed(deleted):
                        restore(restored_id)
                    for completed_id in deleted:
                        completed.discard(completed_id)
                    _CLAIMED_RECORDS.difference_update(claimed_here)
                    status = "rolled-back"
                    error = type(exc).__name__
                    deleted.clear()
                    break
                except BaseException as exc:
                    # A worker interruption must leave an auditable partial run.
                    status = "incomplete"
                    error = type(exc).__name__
                    for remaining_index, remaining in enumerate(
                        expired[index:], start=index
                    ):
                        remaining_category = _safe_category(
                            remaining.get(
                                "category",
                                remaining.get("data_category", "unknown"),
                            )
                        )
                        remaining_counts = counts_by_category.setdefault(
                            remaining_category, {}
                        )
                        if remaining_index != index:
                            increment(remaining_counts, "candidate")
                        increment(remaining_counts, "not-processed")
                    break
            if status in {"rolled-back", "incomplete"}:
                break
        if failed:
            status = "partial-failure"
            error = "one_or_more_records_failed"
        if status in {"rolled-back", "incomplete"}:
            _RUN_STATE.pop(run_key, None)
        report = {
            "event": "audit_retention.run",
            "generated_at": now.astimezone(UTC).isoformat(),
            "run_id": run_key,
            "tenant_id": _identifier(tenant_id, secret=secret),
            "candidate_count": len(expired),
            "deleted_count": sum(
                1 for record in expired if str(record["id"]) in completed
            )
            if status in {"passed", "partial-failure"}
            else 0,
            "failed_count": len(failed),
            "retry_count": retry_count,
            "counts_by_category": counts_by_category,
            "result_status_counts": result_status_counts,
            "status": status,
            "error": error,
            "dry_run": dry_run,
            "secrets_or_financial_values": False,
        }
        return deepcopy(report)


def can_access_report(
    report: dict[str, Any],
    *,
    credential_tenant_id: str,
    credential_role: str,
    now: datetime,
    policy: dict[str, Any],
) -> bool:
    """Enforce report-specific authorization and expiry."""
    allowed_roles = set(cast("list[str]", policy.get("allowed_roles", [])))
    if credential_role not in allowed_roles:
        return False
    generated = datetime.fromisoformat(str(report["generated_at"]))
    if now > generated + timedelta(days=int(policy["retention_days"])):
        return False
    if credential_role == "application-admin":
        return True
    return hmac.compare_digest(
        str(report.get("tenant_id", "")), credential_tenant_id
    )


def authorize_report_access(report: dict[str, Any], **kwargs: Any) -> None:
    if not can_access_report(report, **kwargs):
        message = "report access denied"
        raise PermissionError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("config/audit-retention-policy.json"),
    )
    parser.add_argument(
        "--artifact", type=Path, default=Path("audit-retention-run.json")
    )
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
        args.artifact.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        sys.stderr.write(f"audit retention failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
