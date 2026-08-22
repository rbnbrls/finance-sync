"""Release 16 backup/restore drill contracts."""

# pyright: basic

import json
from pathlib import Path

from scripts.backup_restore_drill import SCHEMA, build_report


def test_report_proves_restore_integrity_and_safe_evidence() -> None:
    report = build_report(
        backup_file="release16.backup",
        domain_rows=2,
        outbox_rows=2,
        tenant_ids=["tenant-beta", "tenant-acme"],
        migration_head="alembic-head",
        duration_seconds=1.2,
    )
    assert report["schema"] == SCHEMA
    assert report["row_counts"] == {"domain": 2, "outbox": 2}
    assert report["tenant_ids"] == ["tenant-acme", "tenant-beta"]
    assert report["constraints_valid"] is True
    assert report["credentials_detected"] is False
    assert report["logs_redacted"] is True
    assert report["rpo_minutes"] <= 15
    json.dumps(report)


def test_ci_runs_isolated_postgres_backup_restore_drill() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    script = Path("scripts/backup_restore_drill.py").read_text(encoding="utf-8")
    assert "backup-restore:" in workflow
    assert "backup_restore_drill.py" in workflow
    assert "pg_dump" in script
    assert "pg_restore" in script
