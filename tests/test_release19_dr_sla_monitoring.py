"""Release 19 DR SLA monitoring contracts.

Covers the acceptance criteria of backlog/release19-dr-sla-monitoring.md:
periodic isolated restore-check, restore-duration / last-usable-backup /
replay-lag / recovery-status metrics, RPO/RTO breach alerts, safe
publication (no tenant data, credentials or financial values), and
failure links to a runbook and owner.

Holdout scenarios covered here (evaluator-provided, not shown to the
implementer):
1. Tenant isolation during restore-check
2. Secret leak through the failure path
3. Injection via tenant_id / runbook_id
4. Negative replay-lag from clock skew
5. Empty backup inventory (false RPO-green)
6. Partial run (crash mid-run)
7. New/empty tenant (zero rows)
8. Alert storm and runbook integrity
"""

# pyright: basic

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.dr_sla_monitoring import (
    CREDENTIAL_PATTERNS,
    detect_previous_run,
    redact_text,
    restore_check,
    runbook_fetch_status,
    validate_config,
)

REPO_ROOT = Path(__file__).parents[1]


def _config() -> dict:
    return json.loads(
        (REPO_ROOT / "config/dr-sla-monitoring.json").read_text(
            encoding="utf-8"
        )
    )


def _backup(
    *,
    backup_id: str,
    tenant: str,
    created_at: datetime,
    usable: bool = True,
) -> dict:
    return {
        "backup_id": backup_id,
        "tenant": tenant,
        "created_at": created_at.isoformat(),
        "usable": usable,
    }


# ── 1. Tenant isolation ───────────────────────────────────────────────


def test_restore_check_reads_only_own_tenant_backups() -> None:
    """A restore-check for tenant A must never read tenant B objects."""
    now = datetime.now(UTC)
    inventory = [
        _backup(
            backup_id="b-a-1",
            tenant="tenant-a",
            created_at=now - timedelta(minutes=5),
        ),
        _backup(
            backup_id="b-b-1",
            tenant="tenant-b",
            created_at=now - timedelta(minutes=2),
        ),
    ]
    report = restore_check(
        tenant_id="tenant-a",
        backups=inventory,
        now=now,
    )
    assert report["tenant"] == "tenant-a"
    # Only tenant-a's backup is eligible; tenant-b's is never selected.
    assert report["last_usable_backup_id"] == "b-a-1"


def test_restore_check_rejects_foreign_tenant_backup_id() -> None:
    """An explicit attempt with a tenant-B backup id fails with zero bytes."""
    now = datetime.now(UTC)
    inventory = [
        _backup(
            backup_id="b-b-1",
            tenant="tenant-b",
            created_at=now - timedelta(minutes=2),
        ),
    ]
    report = restore_check(
        tenant_id="tenant-a",
        backups=inventory,
        requested_backup_id="b-b-1",
        now=now,
    )
    assert report["status"] != "success"
    assert report["bytes_restored"] == 0
    assert "not owned" in report["error"].lower()


# ── 2. Secret leak via failure path ───────────────────────────────────


def test_failure_payload_never_leaks_credentials() -> None:
    """Forced restore-failure payloads must contain zero credential matches."""
    leaked_dsn = "postgres://admin:s3cret@db.internal:5432/finance"
    now = datetime.now(UTC)
    report = restore_check(
        tenant_id="tenant-a",
        backups=[
            _backup(
                backup_id="b-a-1",
                tenant="tenant-a",
                created_at=now - timedelta(minutes=5),
            ),
        ],
        now=now,
        error=(
            f"connection to {leaked_dsn} failed: password=supersecret "
            "Bearer abc.def.ghi sk-1234567890abcdef"
        ),
    )
    assert report["status"] == "failed"
    text = json.dumps(report)
    for pattern in CREDENTIAL_PATTERNS:
        assert re.search(pattern, text) is None, (
            f"credential pattern {pattern!r} leaked into published payload"
        )
    # The sanitised error must still be non-empty and actionable.
    assert report["error"]
    assert "s3cret" not in report["error"]
    assert "supersecret" not in report["error"]


def test_redact_text_strips_nested_credential_fields() -> None:
    nested = {
        "error": {
            "cause": {
                "detail": "postgres://user:pass@host/db password=hunter2 Bearer tok",
            }
        }
    }
    redacted = redact_text(json.dumps(nested))
    assert "user:pass" not in redacted
    assert "hunter2" not in redacted
    assert "Bearer tok" not in redacted


# ── 3. Injection via tenant_id / runbook_id ───────────────────────────


def test_malicious_tenant_id_never_reaches_sql_or_shell() -> None:
    """tenant_id used as SQL/parameter must not escape its tenant scope."""
    now = datetime.now(UTC)
    report = restore_check(
        tenant_id="x' OR 1=1 --",
        backups=[_backup(backup_id="b-a-1", tenant="tenant-a", created_at=now)],
        now=now,
    )
    # No rows from outside the tenant and no successful run: rejected.
    assert report["status"] != "success"
    assert report["bytes_restored"] == 0


def test_malicious_runbook_id_is_rejected_not_executed() -> None:
    """runbook_id used in shell must be rejected, never executed."""
    config = _config()
    config["runbook_id"] = "$(touch /tmp/pwn)"
    with pytest.raises(ValueError, match="runbook_id"):
        validate_config(config)
    assert not Path("/tmp/pwn").exists()


# ── 4. Clock-skew safe replay-lag ─────────────────────────────────────


def test_replay_lag_never_negative_under_clock_skew() -> None:
    """Target clock 5 minutes behind the source must not produce negative lag."""
    source_last_wal = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    target_clock = source_last_wal - timedelta(minutes=5)
    report = restore_check(
        tenant_id="tenant-a",
        backups=[
            _backup(
                backup_id="b-a-1",
                tenant="tenant-a",
                created_at=source_last_wal - timedelta(minutes=3),
            ),
        ],
        now=target_clock,
        source_last_wal=source_last_wal,
    )
    assert report["replay_lag_seconds"] >= 0
    assert abs(report["replay_lag_seconds"] - 3 * 60) <= 60
    # No false RTO alert from a negative lag.
    assert report["alerts"] == []


# ── 5. Empty backup inventory ─────────────────────────────────────────


def test_empty_inventory_never_reports_false_rpo_green() -> None:
    """Zero usable backups must not produce status=success / last_usable=now."""
    now = datetime.now(UTC)
    report = restore_check(tenant_id="tenant-a", backups=[], now=now)
    assert report["status"] != "success"
    assert report["last_usable_backup"] is None
    assert report["last_usable_backup_id"] is None
    # Failure handling with runbook link is triggered, not a success status.
    assert report["runbook_link"]
    assert report["owner"]
    assert report["error"]


# ── 6. Partial run (crash mid-run) ────────────────────────────────────


def test_partial_run_is_detected_and_publishes_unknown() -> None:
    """A run that crashes after duration measurement must not go green."""
    previous = {
        "status": "running",
        "duration_seconds": 12.5,
        "replay_lag_seconds": None,  # never written
        "finished_at": None,
    }
    incomplete = detect_previous_run(previous)
    assert incomplete is True
    now = datetime.now(UTC)
    report = restore_check(
        tenant_id="tenant-a",
        backups=[
            _backup(
                backup_id="b-a-1",
                tenant="tenant-a",
                created_at=now - timedelta(minutes=3),
            ),
        ],
        now=now,
        previous_run=previous,
    )
    assert report["status"] in {"unknown", "failed"}
    assert report["status"] != "success"
    assert "incomplete" in report["error"].lower()


# ── 7. New / empty tenant ─────────────────────────────────────────────


def test_empty_tenant_completes_with_defined_metrics() -> None:
    """A tenant with zero financial rows must finish with defined metrics."""
    now = datetime.now(UTC)
    report = restore_check(
        tenant_id="tenant-empty",
        backups=[
            _backup(
                backup_id="b-empty-1",
                tenant="tenant-empty",
                created_at=now - timedelta(minutes=2),
            ),
        ],
        now=now,
        domain_rows=0,
    )
    assert report["status"] in {"success", "noop"}
    assert report["duration_seconds"] >= 0
    assert "duration_seconds" in report
    assert "replay_lag_seconds" in report
    assert "last_usable_backup" in report
    assert report["bytes_restored"] == 0


# ── 8. Alert storm and runbook integrity ──────────────────────────────


def test_alert_dedup_limits_one_alert_per_interval_per_tenant() -> None:
    """10 consecutive failing runs must not cause an alert storm."""
    config = _config()
    now = datetime.now(UTC)
    previous: dict | None = None
    alert_count = 0
    for _ in range(10):
        report = restore_check(
            tenant_id="tenant-a",
            backups=[],
            now=now,
            previous_run=previous,
            config=config,
        )
        # With a fresh run every time (previous success), each run may alert,
        # but the dedup interval suppresses repeats within the window.
        alert_count += len(report["alerts"])
        previous = report
        now = now + timedelta(minutes=1)
    assert alert_count <= 1


def test_every_alert_carries_runbook_and_owner() -> None:
    config = _config()
    now = datetime.now(UTC)
    report = restore_check(
        tenant_id="tenant-a",
        backups=[],
        now=now,
        config=config,
    )
    assert report["alerts"]
    for alert in report["alerts"]:
        assert alert["runbook_link"]
        assert alert["owner"]
        assert alert["runbook_id"]


def test_runbook_url_returns_200_with_owner_field() -> None:
    """The configured runbook URL must resolve and declare an owner."""
    config = _config()
    status, ok, owner = runbook_fetch_status(config["runbook_url"])
    assert ok, f"runbook fetch failed: {status}"
    assert owner, "runbook body must declare an owner"


# ── Config + CI wiring ────────────────────────────────────────────────


def test_config_is_valid_and_matches_slas() -> None:
    config = _config()
    validate_config(config)
    assert config["rpo_minutes"] == 15
    assert config["rto_minutes"] == 30
    assert config["owner"]
    assert config["runbook_url"].startswith("https://")
    assert config["runbook_id"]


def test_report_contains_no_sensitive_data() -> None:
    config = _config()
    now = datetime.now(UTC)
    report = restore_check(
        tenant_id="tenant-a",
        backups=[
            _backup(
                backup_id="b-a-1",
                tenant="tenant-a",
                created_at=now - timedelta(minutes=3),
            ),
        ],
        now=now,
        config=config,
    )
    text = json.dumps(report)
    # No financial values, no credentials, no raw tenant data.
    assert "amount" not in text.lower()
    for pattern in CREDENTIAL_PATTERNS:
        assert re.search(pattern, text) is None


def test_ci_runs_periodic_isolated_dr_sla_check() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    assert "dr-sla-monitoring:" in workflow
    assert "dr_sla_monitoring.py" in workflow
    assert "dr-sla-monitoring.json" in workflow
    assert "pg_dump" in workflow or "synthetic" in workflow


def test_backlog_story_flipped_to_done() -> None:
    text = (REPO_ROOT / "backlog/release19-dr-sla-monitoring.md").read_text(
        encoding="utf-8"
    )
    assert re.search(r"^status:\s*done\s*$", text, re.MULTILINE)
    assert "## Implementatie en verificatie" in text
    criteria = re.findall(r"^- \[([ x])\]", text, re.MULTILINE)
    assert criteria and all(mark == "x" for mark in criteria)
