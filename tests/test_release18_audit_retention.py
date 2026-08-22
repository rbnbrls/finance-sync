"""Release 18 audit-retention enforcement contracts."""

# pyright: basic

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.enforce_audit_retention import execute_retention, select_expired


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
    assert [item["id"] for item in select_expired(records, now=now, retention_days=3650, tenant_id="tenant-a")] == ["a-old", "a-old-2"]
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
        records, now=now, retention_days=3650, tenant_id="tenant-a", dry_run=False,
        delete=fail_after_first, restore=restored.append,
    )
    assert failed["status"] == "rolled-back"
    assert restored == ["a-old"]
    retried = execute_retention(
        records, now=now, retention_days=3650, tenant_id="tenant-a", dry_run=False,
        delete=lambda record_id: deleted.append(record_id), restore=restored.append,
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
