"""Tenant-scoped sync scheduling model.

One row = one executable source (``ingestion`` + connection id) or one
export destination (``export`` + exporter/target id).  The worker plans
runs exclusively from these rows; the global ``WORKER_JOB_*`` interval
settings remain operational *limits*, not per-connection user settings.

Schedules never carry credentials, provider payloads or financial data.
The ``schedule`` JSONB holds the versioned recurrence description
(``frequency``, ``time``, ``weekdays``, ``interval_hours``); a
``schema_version`` column lets the application evolve the shape without
losing the audit trail of old values.

Time-zone behaviour (documented in ``docs/sync-schedules.md`` and
enforced by ``services/sync_schedule.py``):

* ``timezone`` is an IANA zone name; ``next_run_at`` / ``last_run_at``
  are stored as UTC ``timestamptz``.
* ``weekday`` means Monday-Friday in the schedule's own timezone — no
  national holidays.
* DST transitions never produce a double local run; a non-existent
  local time shifts forward to the first valid instant.
* Missed (misfired) runs are coalesced into at most one safe catch-up
  run per schedule (``last_scheduled_at`` drives the due check).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts

# ── Scope kinds ───────────────────────────────────────────────────────

SCOPE_INGESTION = "ingestion"
SCOPE_EXPORT = "export"
SCOPE_KINDS = {SCOPE_INGESTION, SCOPE_EXPORT}

#: Synced-by-default ingestion providers (mirrors the worker's historical
#: global-interval jobs: bunq transactions + cards, Trading212).
INGESTION_SCHEDULABLE_PROVIDERS = {"bunq", "trading212"}

#: Exporter keys that receive a per-tenant schedule.
EXPORT_SCHEDULABLE_EXPORTERS = {
    "wealthfolio",
    "actual-budget",
    "firefly",
    "ghostfolio",
    "investbrain",
}

# ── Default schedule ──────────────────────────────────────────────────

#: Default: every weekday (Mon-Fri) at 07:00 in the tenant timezone.
DEFAULT_SCHEDULE_TIME = "07:00"
DEFAULT_SCHEDULE_WEEKDAYS = [0, 1, 2, 3, 4]  # Monday=0 … Friday=4
DEFAULT_TIMEZONE = "Europe/Amsterdam"
FALLBACK_TIMEZONE = "UTC"


class SyncSchedule(Base):
    """Per-connection / per-exporter recurrence for scheduled runs."""

    __tablename__ = "sync_schedules"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "scope",
            "target_id",
            name="uq_sync_schedules_tenant_scope_target",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
        comment="Owning tenant; schedules are strictly tenant-scoped",
    )

    # ── Target identity ─────────────────────────────────────────────
    scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="'ingestion' (connector connection) or 'export' (exporter)",
    )
    #: For ingestion: the Credential row id (connection id).  For export:
    #: the exporter key + target id, encoded as ``<exporter>`` for the
    #: single built-in target, or ``<exporter>:<target-id>`` when a
    #: future multi-target exporter needs disambiguation.
    target_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment=(
            "Connection id for ingestion; '<exporter>' or "
            "'<exporter>:<target>' for export"
        ),
    )

    # ── Recurrence ──────────────────────────────────────────────────
    enabled: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default="true",
        comment="False stops scheduled runs; manual runs stay possible",
    )
    schedule: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment=(
            "Versioned recurrence: {frequency: daily|weekdays|weekly|"
            "hourly, time: 'HH:MM', weekdays: [0-6], interval_hours: N}"
        ),
    )
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Version of the schedule JSON shape (audit-friendly)",
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=DEFAULT_TIMEZONE,
        server_default=DEFAULT_TIMEZONE,
        comment=(
            "IANA timezone name (e.g. Europe/Amsterdam); defaults to the "
            "documented tenant timezone"
        ),
    )

    # ── Optimistic concurrency ──────────────────────────────────────
    # Incremented on every change; PATCH/PUT requests carry the version
    # they based their edit on and get a 409 when it is stale, so two
    # concurrent admins cannot silently clobber each other.
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Optimistic-lock version; bumped on every change",
    )

    # ── Computed scheduling state ───────────────────────────────────
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Next planned run (UTC); recomputed on every change",
    )
    last_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Last time the worker actually STARTED a scheduled run "
            "(drives misfire/catch-up coalescing); NULL = never"
        ),
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Start time of the most recent run for this schedule",
    )
    last_run_status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Status of the most recent run: completed/failed/skipped",
    )
    last_run_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Sanitised error of the most recent run (no secrets)",
    )

    # ── Audit ───────────────────────────────────────────────────────
    created_by: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Actor (user or api-key id) that created the schedule",
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Actor that last changed the schedule",
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()

    def __repr__(self) -> str:
        return (
            f"<SyncSchedule id={self.id!r} scope={self.scope!r} "
            f"target={self.target_id!r} enabled={self.enabled}>"
        )
