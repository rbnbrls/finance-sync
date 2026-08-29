"""Release 18 audit-retention enforcement contracts."""

# pyright: basic

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.enforce_audit_retention import (
    execute_retention,
    select_expired,
)


def _records(now: datetime) -> list[dict[str, str]]:
    old = (now - timedelta(days=4000)).isoformat()
    fresh = (now - timedelta(days=2)).isoformat()
    return [
        {"id": "a-old", "tenant_id": "tenant-a", "created_at": old},
        {"id": "a-old-2", "tenant_id": "tenant-a", "created_at": old},
        {"id": "b-old", "tenant_id": "tenant-b", "created_at": old},
        {"id": "a-fresh", "tenant_id": "tenant-a", "created_at": fresh},
    ]


def test_retention_is_tenant_scoped_and_dry_run_safe() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    records = _records(now)
    assert [
        item["id"]
        for item in select_expired(
            records, now=now, retention_days=3650, tenant_id="tenant-a"
        )
    ] == ["a-old", "a-old-2"]
    report = execute_retention(
        records,
        now=now,
        retention_days=3650,
        tenant_id="tenant-a",
        dry_run=True,
        delete=lambda _record_id: pytest.fail("dry-run deleted a record"),
        restore=lambda _record_id: None,
    )
    assert report["candidate_count"] == 2
    assert report["deleted_count"] == 0
    assert report["status"] == "dry-run"
    assert report["secrets_or_financial_values"] is False


def test_failure_rolls_back_and_retry_can_succeed() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    records = _records(now)
    deleted: list[str] = []
    restored: list[str] = []

    def fail_after_first(record_id: str) -> None:
        deleted.append(record_id)
        if len(deleted) == 1:
            return
        message = "storage failure"
        raise RuntimeError(message)

    failed = execute_retention(
        records,
        now=now,
        retention_days=3650,
        tenant_id="tenant-a",
        dry_run=False,
        delete=fail_after_first,
        restore=restored.append,
    )
    assert failed["status"] == "rolled-back"
    assert restored == ["a-old"]
    retried = execute_retention(
        records,
        now=now,
        retention_days=3650,
        tenant_id="tenant-a",
        dry_run=False,
        delete=lambda record_id: deleted.append(record_id),
        restore=restored.append,
    )
    assert retried["status"] == "passed"
    assert retried["deleted_count"] == 2


def test_policy_and_ci_contract_are_present() -> None:
    policy = json.loads(Path("config/audit-retention-policy.json").read_text())
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert policy["tenant_scoped"] is True
    assert policy["schema_migration_required"] is False
    assert "audit-retention:" in workflow
    assert "enforce_audit_retention.py" in workflow


def test_report_has_category_status_counts_and_only_irreversible_identifiers() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        {"id": "secret-a", "tenant_id": "tenant-a", "category": "events", "created_at": (now - timedelta(days=4000)).isoformat()},
        {"id": "secret-b", "tenant_id": "tenant-a", "category": "sessions", "created_at": (now - timedelta(days=4000)).isoformat()},
    ]
    report = execute_retention(
        records, now=now, retention_days=3650, tenant_id="tenant-a", dry_run=True,
        delete=lambda _: None, restore=lambda _: None,
    )
    assert report["run_id"] != "tenant-a"
    assert report["tenant_id"] != "tenant-a"
    assert report["counts_by_category"] == {"events": {"candidate": 1, "dry-run": 1}, "sessions": {"candidate": 1, "dry-run": 1}}
    assert report["result_status_counts"] == {"dry-run": 2}
    serialized = json.dumps(report)
    assert "secret-a" not in serialized and "secret-b" not in serialized


def test_execute_retries_transient_delete_and_exposes_partial_failure_without_ids() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    records = _records(now)
    attempts: dict[str, int] = {}

    def flaky(record_id: str) -> None:
        attempts[record_id] = attempts.get(record_id, 0) + 1
        if record_id == "a-old-2":
            message = "storage failure"
            raise RuntimeError(message)

    report = execute_retention(
        records, now=now, retention_days=3650, tenant_id="tenant-a", dry_run=False,
        delete=flaky, restore=lambda _: None, max_retries=2,
    )
    assert report["status"] == "partial-failure"
    assert report["retry_count"] == 2
    assert report["result_status_counts"] == {"deleted": 1, "failed": 1}
    assert report["failed_count"] == 1
    assert "a-old-2" not in json.dumps(report)


def test_report_retention_policy_is_separate_and_limited() -> None:
    policy = json.loads(Path("config/report-retention-policy.json").read_text())
    assert policy["retention_days"] < json.loads(Path("config/audit-retention-policy.json").read_text())["retention_days"]
    assert policy["purpose"] == "retention-run-reports"
