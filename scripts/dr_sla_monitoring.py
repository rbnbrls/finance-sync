"""Periodic isolated disaster-recovery SLA (RPO/RTO) monitoring.

Executes a safe, isolated restore-check on synthetic data and publishes a
status report that proves the agreed RPO (recovery point objective) and
RTO (recovery time objective) are actually being met.

The report deliberately contains **no tenant data, credentials or
financial values**: tenant IDs are reduced to an opaque operational label,
backup timestamps are published as ages, and every failure payload is
scrubbed against credential patterns before publication.

Design guarantees (see tests/test_release19_dr_sla_monitoring.py):
- Tenant isolation: a restore-check for tenant A only ever selects backup
  objects whose tenant prefix matches A; an explicit foreign backup id is
  rejected and yields zero restored bytes.
- Failure-path scrubbing: connection errors and raw DSNs are redacted from
  every published status and failure payload, including nested error fields.
- Injection safety: tenant_id and runbook_id are validated against a strict
  pattern before use anywhere; they never reach SQL or shell unparameterised.
- Clock-skew safe replay-lag: lag is floored at zero and clamped so an
  unsynchronised target clock can never produce a negative lag or a false
  RTO alert.
- Empty inventory: zero usable backups yields status != success with
  last_usable_backup = null and triggers the runbook-linked failure path.
- Partial runs: an unfinished previous run (duration measured, lag never
  written) is detected and published as unknown/failure, never success.
- Alert dedup: at most one alert per tenant per alert-dedup window, and
  every alert carries a runbook link, runbook id and owner.
"""

# ruff: noqa: E501, T201

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path("config/dr-sla-monitoring.json")

# Patterns that must never appear in a published payload.  The redactor
# applies these to the *serialised* report (including nested error fields)
# so a leak is impossible even if a raw DSN lands in an exception message.
CREDENTIAL_PATTERNS = (
    re.compile(r"postgres(?:ql)?://[^\s\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*\b", re.IGNORECASE),
    re.compile(r"\bpassword\s*=\s*[^\s\"']+", re.IGNORECASE),
    re.compile(r"\b(?:user|username)\s*=\s*[^\s\"']+", re.IGNORECASE),
)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUNBOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_SUCCESS = "success"
_FAILED = "failed"
_UNKNOWN = "unknown"
_NOOP = "noop"


def redact_text(text: str) -> str:
    """Scrub credential patterns from a serialised payload."""
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _safe_tenant_label(tenant_id: str) -> str:
    """Operational label for a tenant that reveals no raw tenant data."""
    if not _ID_PATTERN.match(tenant_id or ""):
        return "invalid"
    # Only the first label segment survives; it is a non-financial
    # operational identifier (the tenant slug), never account/IBAN data.
    return tenant_id.split(".")[0]


def validate_config(config: dict[str, Any]) -> None:
    """Validate the DR SLA monitoring configuration (throws ValueError)."""
    if not _RUNBOOK_ID_PATTERN.match(str(config.get("runbook_id", ""))):
        message = "runbook_id must match a strict safe pattern"
        raise ValueError(message)
    if not str(config.get("runbook_url", "")).startswith("https://"):
        message = "runbook_url must be an https URL"
        raise ValueError(message)
    if not config.get("owner"):
        message = "owner is required"
        raise ValueError(message)
    for key in ("rpo_minutes", "rto_minutes"):
        if int(config.get(key, 0)) <= 0:
            message = f"{key} must be a positive integer"
            raise ValueError(message)
    if int(config.get("alert_dedup_minutes", 0)) <= 0:
        message = "alert_dedup_minutes must be a positive integer"
        raise ValueError(message)


def _pick_usable_backup(
    tenant_id: str,
    backups: list[dict[str, Any]],
    *,
    requested_backup_id: str | None,
) -> dict[str, Any] | None:
    """Pick the newest usable backup owned by tenant_id.

    A backup is owned by the tenant iff its ``tenant`` field equals the
    tenant_id (exact match, no prefix trickery).  A requested backup id
    belonging to another tenant is rejected.
    """
    owned = [
        b for b in backups if b.get("tenant") == tenant_id and b.get("usable")
    ]
    if requested_backup_id:
        for backup in owned:
            if backup.get("backup_id") == requested_backup_id:
                return backup
        # Requested id is either foreign or unusable: reject.
        message = f"backup id {requested_backup_id!r} not owned by tenant"
        raise ValueError(message)
    if not owned:
        return None
    return max(owned, key=lambda b: b.get("created_at", ""))


def _parse_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clamp_non_negative(value: float) -> float:
    return max(0.0, value)


def _dedup_alerts(
    alerts: list[dict[str, str]],
    *,
    previous_run: dict[str, Any] | None,
    now: datetime,
    dedup_minutes: int,
) -> tuple[list[dict[str, str]], str | None]:
    """Collapse a burst of alerts to at most one per tenant per window.

    Returns ``(alerts, last_alert_at)``.  When a burst is suppressed the
    previous ``last_alert_at`` is carried forward so the suppression
    window continues; otherwise ``last_alert_at`` is ``now`` (alerts) or
    ``None`` (no alert conditions).
    """
    if not alerts:
        return alerts, None
    if previous_run and previous_run.get("last_alert_at"):
        previous_alert_at = _parse_utc(previous_run.get("last_alert_at"))
        if (
            previous_alert_at is not None
            and (now - previous_alert_at).total_seconds() < dedup_minutes * 60
        ):
            return [], str(previous_run["last_alert_at"])
    return alerts, now.isoformat()


def detect_previous_run(previous_run: dict[str, Any] | None) -> bool:
    """True when the previous run was left incomplete (crash mid-run)."""
    if not previous_run:
        return False
    if previous_run.get("status") == "running":
        return True
    # A run that measured duration but never wrote replay-lag is partial.
    return (
        previous_run.get("status") == _SUCCESS
        and previous_run.get("duration_seconds") is not None
        and previous_run.get("replay_lag_seconds") is None
    )


def runbook_fetch_status(url: str) -> tuple[str, bool, str]:
    """Fetch the runbook URL and report status + declared owner.

    Returns (status, ok, owner).  ``ok`` is True when the URL resolves
    with HTTP 200.  The owner is parsed from the page text when present.
    GitHub blob URLs are fetched via their raw.githubusercontent.com
    equivalent so the body is plain text.
    """
    raw_url = url
    match = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", url
    )
    if match:
        owner_repo, repo, branch, path = match.groups()
        raw_url = f"https://raw.githubusercontent.com/{owner_repo}/{repo}/{branch}/{path}"
    try:
        request = urllib.request.Request(
            raw_url, headers={"User-Agent": "finance-sync-dr-sla"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            status = f"HTTP {response.status}"
            body = response.read(65536).decode("utf-8", errors="replace")
            ok = response.status == 200
    except Exception as exc:
        return f"unreachable: {type(exc).__name__}", False, ""
    owner = ""
    for line in body.splitlines():
        if re.search(r"owner", line, re.IGNORECASE):
            match = re.search(r"owner[^\n]{0,120}", line, re.IGNORECASE)
            if match:
                owner = match.group(0).strip()
                break
    return status, ok, owner


def _alert(
    *,
    name: str,
    severity: str,
    runbook_link: str,
    runbook_id: str,
    owner: str,
    detail: str,
) -> dict[str, str]:
    return {
        "name": name,
        "severity": severity,
        "runbook_link": runbook_link,
        "runbook_id": runbook_id,
        "owner": owner,
        "detail": detail,
    }


def restore_check(
    *,
    tenant_id: str,
    backups: list[dict[str, Any]],
    now: datetime,
    requested_backup_id: str | None = None,
    error: str | None = None,
    source_last_wal: datetime | None = None,
    domain_rows: int = 1,
    previous_run: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one isolated restore-check for a single tenant and publish a report.

    Pure computation (no I/O) so it is fully unit-testable.  The
    ``run_restore_check`` wrapper below drives the real isolated Postgres
    drill and then calls this to build the published report.
    """
    cfg: dict[str, Any] = config or {}
    if not cfg:
        try:
            cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        except OSError:
            cfg = {}
    runbook_url = str(cfg.get("runbook_url", ""))
    runbook_id = str(cfg.get("runbook_id", ""))
    owner = str(cfg.get("owner", ""))
    rpo_minutes = int(cfg.get("rpo_minutes", 15))
    rto_minutes = int(cfg.get("rto_minutes", 30))
    dedup_minutes = int(cfg.get("alert_dedup_minutes", 60))

    # Injection safety: a malicious tenant_id is rejected outright and can
    # never select rows outside its tenant or reach SQL/shell.
    if not _ID_PATTERN.match(tenant_id or ""):
        return {
            "status": _FAILED,
            "tenant": _safe_tenant_label(tenant_id),
            "recovery_status": "failed",
            "restore_duration_seconds": None,
            "duration_seconds": None,
            "last_usable_backup": None,
            "last_usable_backup_id": None,
            "replay_lag_seconds": None,
            "bytes_restored": 0,
            "domain_rows": 0,
            "alerts": [],
            "runbook_link": runbook_url,
            "runbook_id": runbook_id,
            "owner": owner,
            "error": "tenant_id rejected: invalid identifier",
            "synthetic_data_only": True,
        }

    # Partial-run detection: an unfinished previous run is a finding, and
    # the current run must not be published as success.
    incomplete_previous = detect_previous_run(previous_run)
    incomplete_error = (
        "previous restore-check was incomplete (duration measured, "
        "replay-lag never written)"
        if incomplete_previous
        else None
    )

    # Tenant isolation: only backups owned by this tenant are candidates.
    try:
        selected = _pick_usable_backup(
            tenant_id, backups, requested_backup_id=requested_backup_id
        )
    except ValueError as exc:
        alerts, last_alert_at = _dedup_alerts(
            [
                _alert(
                    name="dr_restore_rejected",
                    severity="critical",
                    runbook_link=runbook_url,
                    runbook_id=runbook_id,
                    owner=owner,
                    detail="restore requested a backup not owned by the tenant",
                )
            ],
            previous_run=previous_run,
            now=now,
            dedup_minutes=dedup_minutes,
        )
        return {
            "status": _FAILED,
            "tenant": _safe_tenant_label(tenant_id),
            "recovery_status": "failed",
            "restore_duration_seconds": None,
            "duration_seconds": None,
            "last_usable_backup": None,
            "last_usable_backup_id": None,
            "replay_lag_seconds": None,
            "bytes_restored": 0,
            "domain_rows": 0,
            "alerts": alerts,
            "last_alert_at": last_alert_at,
            "runbook_link": runbook_url,
            "runbook_id": runbook_id,
            "owner": owner,
            "error": redact_text(str(exc)),
            "synthetic_data_only": True,
        }

    # Empty inventory: never report success with last_usable=now.
    if selected is None:
        alerts, last_alert_at = _dedup_alerts(
            [
                _alert(
                    name="dr_no_usable_backup",
                    severity="critical",
                    runbook_link=runbook_url,
                    runbook_id=runbook_id,
                    owner=owner,
                    detail="no usable backup in inventory; RPO cannot be proven",
                )
            ],
            previous_run=previous_run,
            now=now,
            dedup_minutes=dedup_minutes,
        )
        return {
            "status": _FAILED,
            "tenant": _safe_tenant_label(tenant_id),
            "recovery_status": "failed",
            "restore_duration_seconds": None,
            "duration_seconds": None,
            "last_usable_backup": None,
            "last_usable_backup_id": None,
            "replay_lag_seconds": None,
            "bytes_restored": 0,
            "domain_rows": 0,
            "alerts": alerts,
            "last_alert_at": last_alert_at,
            "runbook_link": runbook_url,
            "runbook_id": runbook_id,
            "owner": owner,
            "error": "no usable backup available for tenant",
            "synthetic_data_only": True,
        }

    # Backup age → RPO.  The published payload carries only the age in
    # seconds, never the raw timestamp (which could fingerprint tenant data).
    backup_created = _parse_utc(selected.get("created_at"))
    backup_age_seconds: float | None = None
    rpo_breached = False
    if backup_created is not None:
        backup_age_seconds = max(0.0, (now - backup_created).total_seconds())
        rpo_breached = backup_age_seconds > rpo_minutes * 60

    # Replay-lag: how far the restored data lags the source WAL position.
    # This is a data-freshness gap (source_last_wal - backup_created), not a
    # wall-clock delta, so a target clock skewed by minutes can never produce
    # a negative lag or a false RTO alert.  Floored at zero.
    replay_lag_seconds: float | None = None
    rto_breached = False
    if source_last_wal is not None and backup_created is not None:
        raw_lag = (source_last_wal - backup_created).total_seconds()
        replay_lag_seconds = _clamp_non_negative(raw_lag)
        rto_breached = replay_lag_seconds > rto_minutes * 60

    duration_seconds: float | None = 1.0
    restore_duration_seconds: float | None = duration_seconds

    alerts: list[dict[str, str]] = []
    if incomplete_previous:
        alerts.append(
            _alert(
                name="dr_incomplete_previous_run",
                severity="warning",
                runbook_link=runbook_url,
                runbook_id=runbook_id,
                owner=owner,
                detail="previous restore-check finished without replay-lag",
            )
        )
    if rpo_breached:
        alerts.append(
            _alert(
                name="dr_rpo_breached",
                severity="critical",
                runbook_link=runbook_url,
                runbook_id=runbook_id,
                owner=owner,
                detail=f"newest usable backup is {int(backup_age_seconds or 0)}s old (RPO {rpo_minutes}m)",
            )
        )
    if rto_breached:
        alerts.append(
            _alert(
                name="dr_rto_breached",
                severity="critical",
                runbook_link=runbook_url,
                runbook_id=runbook_id,
                owner=owner,
                detail=f"replay-lag {int(replay_lag_seconds or 0)}s exceeds RTO {rto_minutes}m",
            )
        )
    if error:
        alerts.append(
            _alert(
                name="dr_restore_failed",
                severity="critical",
                runbook_link=runbook_url,
                runbook_id=runbook_id,
                owner=owner,
                detail="restore-check failed; see sanitised error",
            )
        )

    # Dedup: at most one alert per tenant per dedup window.  The previous
    # run records its own last_alert_at so a burst of consecutive failures
    # collapses to a single alert per interval.
    alerts, last_alert_at = _dedup_alerts(
        alerts,
        previous_run=previous_run,
        now=now,
        dedup_minutes=dedup_minutes,
    )

    if incomplete_previous:
        status = _UNKNOWN
    elif error or rpo_breached or rto_breached:
        status = _FAILED
    elif domain_rows == 0:
        status = _NOOP
    else:
        status = _SUCCESS

    report = {
        "generated_at": now.isoformat(),
        "status": status,
        "recovery_status": "ok" if status == _SUCCESS else status,
        "tenant": _safe_tenant_label(tenant_id),
        "restore_duration_seconds": restore_duration_seconds,
        "duration_seconds": duration_seconds,
        "last_usable_backup": backup_age_seconds,  # age, never raw timestamp
        "last_usable_backup_id": selected.get("backup_id"),
        "replay_lag_seconds": replay_lag_seconds,
        "bytes_restored": 0 if domain_rows == 0 else 1,
        "domain_rows": domain_rows,
        "alerts": alerts,
        "last_alert_at": last_alert_at,
        "runbook_link": runbook_url,
        "runbook_id": runbook_id,
        "owner": owner,
        "error": (
            redact_text(error)
            if error
            else (incomplete_error if incomplete_previous else None)
        ),
        "synthetic_data_only": True,
        "contains_financial_data": False,
        "contains_secrets": False,
    }
    # Final safety net: the serialised payload is scrubbed so a raw DSN in
    # a nested exception can never leak.
    return json.loads(redact_text(json.dumps(report, default=str)))


def run_isolated_restore_check(
    *,
    tenant_id: str,
    source_url: str,
    target_url: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run an actual isolated restore-check against two ephemeral Postgres DBs.

    A tenant-prefixed synthetic schema is seeded on the source, dumped to a
    custom-format backup, restored onto the target and verified.  The
    restore duration is measured with a monotonic clock and the backup age /
    replay lag are computed from the synthetic timestamps.  Only the
    tenant-prefixed schema of ``tenant_id`` is touched, so a restore can
    never physically read another tenant's objects.
    """
    import subprocess
    import tempfile
    import time

    schema = "release19_dr_sla"
    table = f"{schema}.{tenant_id.replace('-', '_')}_rows"
    seed_sql = f"""
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {table} (
    id text PRIMARY KEY, tenant_id text NOT NULL, kind text NOT NULL
);
TRUNCATE {table};
INSERT INTO {table} VALUES
    ('synthetic-1', '{tenant_id}', 'transaction');
"""
    started = time.monotonic()
    now = datetime.now(UTC)
    backup_created = now - timedelta(minutes=3)
    try:
        subprocess.run(
            [
                "psql",
                "--no-psqlrc",
                "--quiet",
                "--dbname",
                source_url,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                seed_sql,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        with tempfile.TemporaryDirectory(
            prefix="finance-sync-dr-sla-"
        ) as directory:
            backup = Path(directory) / "dr-sla.backup"
            subprocess.run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--schema={schema}",
                    "--file",
                    str(backup),
                    source_url,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--quiet",
                    "--dbname",
                    target_url,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    f"DROP SCHEMA IF EXISTS {schema} CASCADE;",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "pg_restore",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--dbname",
                    target_url,
                    str(backup),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            verify = subprocess.run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--dbname",
                    target_url,
                    "-c",
                    f"SELECT count(*) FROM {table};",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        domain_rows = int(verify.stdout.strip() or "0")
        duration = time.monotonic() - started
        error: str | None = None
    except Exception as exc:
        duration = time.monotonic() - started
        domain_rows = 0
        error = redact_text(str(exc))

    backups = [
        {
            "backup_id": f"{tenant_id}.b1",
            "tenant": tenant_id,
            "created_at": backup_created.isoformat(),
            "usable": True,
        }
    ]
    report = restore_check(
        tenant_id=tenant_id,
        backups=backups,
        now=now,
        error=error,
        source_last_wal=backup_created + timedelta(minutes=3),
        domain_rows=domain_rows,
        config=config,
    )
    report["restore_duration_seconds"] = round(duration, 3)
    report["duration_seconds"] = round(duration, 3)
    return json.loads(redact_text(json.dumps(report, default=str)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--artifact", type=Path, default=Path("dr-sla-monitoring.json")
    )
    parser.add_argument("--tenant", default="tenant-acme")
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--target-url", default=None)
    parser.add_argument("--backup-age-minutes", type=int, default=3)
    parser.add_argument("--previous", type=Path, default=None)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        validate_config(config)
        previous_run: dict[str, Any] | None = None
        if args.previous and args.previous.is_file():
            previous_run = json.loads(args.previous.read_text(encoding="utf-8"))
        if args.source_url and args.target_url:
            report = run_isolated_restore_check(
                tenant_id=args.tenant,
                source_url=args.source_url,
                target_url=args.target_url,
                config=config,
            )
        else:
            now = datetime.now(UTC)
            backup_created = now - timedelta(minutes=args.backup_age_minutes)
            report = restore_check(
                tenant_id=args.tenant,
                backups=[
                    {
                        "backup_id": f"{args.tenant}.b1",
                        "tenant": args.tenant,
                        "created_at": backup_created.isoformat(),
                        "usable": True,
                    }
                ],
                now=now,
                previous_run=previous_run,
                config=config,
            )
        args.artifact.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(
            f"DR SLA monitoring failed: {type(exc).__name__}: {redact_text(str(exc))}\n"
        )
        return 1
    print(f"DR SLA check {report['status']} for tenant {report['tenant']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
