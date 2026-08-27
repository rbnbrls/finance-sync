"""Tenant-scoped sync-schedule service.

Owns the lifecycle of :class:`SyncSchedule` rows:

* **Default creation** — every new active ingestion connection and every
  configured export target receives an enabled default schedule
  (weekdays Mon-Fri 07:00 in the tenant timezone, ``UTC`` fallback).
  The worker also lazily creates a default row the first time it sees a
  schedulable connection/target without one, so pre-migration
  configurations and configurations created through other code paths
  converge to the same default.
* **CRUD** — list/read/update/reset/enable/disable, always scoped to the
  caller's tenant; foreign ids return the same 404 as a missing id (no
  existence leak).
* **Preview** — server-computed next three instants (same computation the
  worker uses, so the UI preview always matches the scheduler).
* **Audit** — every change records actor + old/new schedule + timestamp
  in the connection audit log with the shared secret-redaction helpers;
  the schedule row itself never contains credentials or provider
  payloads.

Concurrency: updates carry an optimistic ``version``; a stale version
raises :class:`ScheduleConflictError` (mapped to HTTP 409).  The unique
constraint ``(tenant_id, scope, target_id)`` guarantees at most one row
per scope even under parallel creation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from finance_sync.models import ConnectionAuditLog
from finance_sync.models.sync_schedule import (
    DEFAULT_SCHEDULE_TIME,
    DEFAULT_SCHEDULE_WEEKDAYS,
    DEFAULT_TIMEZONE,
    EXPORT_SCHEDULABLE_EXPORTERS,
    FALLBACK_TIMEZONE,
    INGESTION_SCHEDULABLE_PROVIDERS,
    SCOPE_EXPORT,
    SCOPE_INGESTION,
    SyncSchedule,
)
from finance_sync.sync.schedule_spec import (
    ScheduleValidationError,
    human_readable,
    next_run_instants,
    sanitise_schedule,
    validate_schedule,
    validate_timezone,
)
from finance_sync.utils.redaction import redact_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Max audit detail payload size (mirrors connection_audit service).
_MAX_DETAIL_CHARS = 2000

#: How far ahead (in days) the worker may coalesce a missed run into one
#: catch-up.  A schedule more than this many days overdue is reset to the
#: next future instant instead of firing a catch-up.
CATCHUP_MAX_DELAY_DAYS = 7

__all__ = [
    "CATCHUP_MAX_DELAY_DAYS",
    "ScheduleConflictError",
    "ScheduleNotFoundError",
    "SyncScheduleService",
    "compute_next_run",
    "ensure_schedule_for_connection",
    "ensure_schedule_for_exporter",
    "resolve_tenant_timezone",
]


class ScheduleNotFoundError(LookupError):
    """Raised when a schedule id is missing or belongs to another tenant."""


class ScheduleConflictError(ValueError):
    """Raised when an optimistic-lock version is stale (HTTP 409)."""


# ── Pure helpers ──────────────────────────────────────────────────────


def resolve_tenant_timezone(
    tenant_timezone: str | None = None,
    *,
    fallback: str = DEFAULT_TIMEZONE,
) -> str:
    """Resolve the effective timezone for a schedule.

    Uses *tenant_timezone* when it is a valid IANA zone, otherwise the
    documented default (``Europe/Amsterdam``) and finally ``UTC`` when
    even the default cannot resolve (defensive; the default always
    resolves in practice).
    """
    for candidate in (tenant_timezone, fallback, FALLBACK_TIMEZONE):
        if not candidate:
            continue
        try:
            validate_timezone(candidate)
            return candidate
        except ScheduleValidationError:
            continue
    return FALLBACK_TIMEZONE


def _now() -> datetime:
    """Timezone-aware current UTC instant (single point of control)."""
    return datetime.now(UTC)


def compute_next_run(
    schedule: SyncSchedule | dict[str, Any],
    *,
    after: datetime | None = None,
    count: int = 1,
    timezone: str | None = None,
) -> list[datetime] | None:
    """Compute the next *count* UTC instants for *schedule*.

    Returns ``None`` when the schedule is disabled (no future runs) or
    invalid (the row is corrupt — the worker treats it as not schedulable
    and the API surfaces the validation error separately).

    *timezone* overrides the schedule's own timezone (used by callers
    that pass a plain dict without an embedded timezone).
    """
    if isinstance(schedule, SyncSchedule):
        if not schedule.enabled:
            return None
        spec = schedule.schedule
        tz = timezone or schedule.timezone
    else:
        spec = schedule
        tz = timezone or str(schedule.get("timezone") or FALLBACK_TIMEZONE)
    if not spec:
        return None
    try:
        after_ts = after or _now()
        return next_run_instants(spec, timezone=tz, after=after_ts, count=count)
    except ScheduleValidationError:
        return None


def _default_schedule_payload() -> dict[str, Any]:
    """The default recurrence dict (weekdays 07:00, schema v1)."""
    return {
        "frequency": "weekdays",
        "time": DEFAULT_SCHEDULE_TIME,
        "weekdays": list(DEFAULT_SCHEDULE_WEEKDAYS),
    }


def _sanitise_audit_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Scrub secret-like values from an audit detail payload.

    Recurses through nested dicts/lists: every string value is passed
    through the shared secret redactor and dict values are recursed, so
    a stray token in a label or schedule field can never reach the
    audit log.  Schedule sub-dicts are additionally allowlist-filtered
    by the caller (``sanitise_schedule``) so unknown fields are dropped
    before this pass.
    """
    sanitised: dict[str, Any] = {}
    for key, value in detail.items():
        sanitised[key] = _sanitise_value(value)
    return sanitised


def _sanitise_value(value: Any) -> Any:
    """Recursively redact secret-like strings inside *value*."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_sanitise_value(item) for item in cast("list[Any]", value)]
    if isinstance(value, dict):
        return {
            k: _sanitise_value(v)
            for k, v in cast("dict[str, Any]", value).items()
        }
    return value


# ── Service ───────────────────────────────────────────────────────────


class SyncScheduleService:
    """CRUD + preview + audit for tenant-scoped sync schedules."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Queries ─────────────────────────────────────────────────────

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        scope: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SyncSchedule]:
        """List the tenant's schedules, newest first."""
        stmt = (
            select(SyncSchedule)
            .where(SyncSchedule.tenant_id == tenant_id)
            .order_by(SyncSchedule.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if scope is not None:
            stmt = stmt.where(SyncSchedule.scope == scope)
        rows = await self._session.scalars(stmt)
        return list(rows.all())

    async def get_for_tenant(
        self,
        tenant_id: str,
        schedule_id: str,
    ) -> SyncSchedule:
        """Fetch one schedule scoped to *tenant_id*.

        A foreign or missing id raises :class:`ScheduleNotFoundError` —
        callers never learn whether the id exists in another tenant.
        """
        stmt = select(SyncSchedule).where(
            SyncSchedule.id == schedule_id,
            SyncSchedule.tenant_id == tenant_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            msg = "Schedule not found"
            raise ScheduleNotFoundError(msg)
        return row

    # ── Preview ─────────────────────────────────────────────────────

    async def preview(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        count: int = 3,
    ) -> tuple[SyncSchedule, list[datetime]]:
        """Return the schedule and its next *count* instants.

        The preview is computed with the same pure function the worker
        uses (``compute_next_run``), so the UI always shows exactly what
        the scheduler will plan.  Raises
        :class:`ScheduleValidationError` when the stored schedule is
        corrupt (the API maps it to a 422).
        """
        schedule = await self.get_for_tenant(tenant_id, schedule_id)
        instants = compute_next_run(schedule, count=count) or []
        return schedule, instants

    # ── Mutations ───────────────────────────────────────────────────

    async def update(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        schedule: dict[str, Any] | None = None,
        timezone: str | None = None,
        enabled: bool | None = None,
        version: int | None = None,
        actor_user_id: str | None = None,
        actor_role: str | None = None,
        actor_api_key_id: str | None = None,
    ) -> SyncSchedule:
        """Update schedule fields, recompute ``next_run_at``, audit.

        *version* is the optimistic-lock value the caller based its edit
        on; when it does not match the current row version a
        :class:`ScheduleConflictError` (HTTP 409) is raised and nothing
        is changed.
        """
        row = await self.get_for_tenant(tenant_id, schedule_id)

        if version is not None and version != row.version:
            msg = (
                f"Schedule was modified by another actor "
                f"(expected version {version}, current {row.version})"
            )
            raise ScheduleConflictError(msg)

        old_schedule = dict(row.schedule)
        old_timezone = row.timezone
        old_enabled = row.enabled

        new_schedule = row.schedule
        if schedule is not None:
            # Validate + normalise before touching the row.
            new_schedule = validate_schedule(
                schedule,
                timezone=timezone or row.timezone,
            )
        new_timezone = row.timezone
        if timezone is not None:
            validate_timezone(timezone)
            new_timezone = timezone
        new_enabled = row.enabled if enabled is None else enabled

        changed = (
            new_schedule != old_schedule
            or new_timezone != old_timezone
            or new_enabled != old_enabled
        )
        if not changed:
            return row

        row.schedule = new_schedule
        row.timezone = new_timezone
        row.enabled = new_enabled
        row.version = (row.version or 0) + 1
        row.updated_by = actor_user_id or actor_api_key_id
        row.updated_at = _now()
        # Recompute the next run immediately on every change (acceptance
        # criterion: a change/enable/disable recomputes next_run_at right
        # away).  When disabled there is no next run.
        row.next_run_at = (
            (compute_next_run(row, count=1) or [None])[0]
            if new_enabled
            else None
        )
        await self._session.flush()

        await self._audit(
            tenant_id=tenant_id,
            schedule=row,
            action="schedule.update",
            old={"schedule": old_schedule, "timezone": old_timezone},
            new={"schedule": new_schedule, "timezone": new_timezone},
            changed_fields={
                "schedule": new_schedule != old_schedule,
                "timezone": new_timezone != old_timezone,
                "enabled": new_enabled != old_enabled,
            },
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_api_key_id=actor_api_key_id,
        )
        return row

    async def reset_to_default(
        self,
        tenant_id: str,
        schedule_id: str,
        *,
        version: int | None = None,
        actor_user_id: str | None = None,
        actor_role: str | None = None,
        actor_api_key_id: str | None = None,
    ) -> SyncSchedule:
        """Restore the default schedule (weekdays 07:00, tenant tz)."""
        return await self.update(
            tenant_id,
            schedule_id,
            schedule=_default_schedule_payload(),
            timezone=resolve_tenant_timezone(),
            enabled=True,
            version=version,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_api_key_id=actor_api_key_id,
        )

    # ── Default creation ────────────────────────────────────────────

    async def ensure_for_scope(
        self,
        tenant_id: str,
        *,
        scope: str,
        target_id: str,
        timezone: str | None = None,
        actor_user_id: str | None = None,
    ) -> SyncSchedule:
        """Atomically ensure an enabled default schedule for a scope.

        Creates the row when absent; returns the existing row otherwise.
        The unique constraint ``(tenant_id, scope, target_id)`` makes the
        creation race-safe: a concurrent insert that wins the race makes
        this call re-read and return the winner.
        """
        stmt = select(SyncSchedule).where(
            SyncSchedule.tenant_id == tenant_id,
            SyncSchedule.scope == scope,
            SyncSchedule.target_id == target_id,
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        effective_tz = resolve_tenant_timezone(timezone)
        row = SyncSchedule(
            tenant_id=tenant_id,
            scope=scope,
            target_id=target_id,
            enabled=True,
            schedule=_default_schedule_payload(),
            schema_version=1,
            timezone=effective_tz,
            version=1,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        row.next_run_at = (compute_next_run(row, count=1) or [None])[0]
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            # Lost the race — another worker/request created the row.
            await self._session.rollback()
            stmt = select(SyncSchedule).where(
                SyncSchedule.tenant_id == tenant_id,
                SyncSchedule.scope == scope,
                SyncSchedule.target_id == target_id,
            )
            winner = (await self._session.execute(stmt)).scalar_one_or_none()
            if winner is not None:
                return winner
            raise
        return row

    # ── Audit ───────────────────────────────────────────────────────

    async def _audit(
        self,
        *,
        tenant_id: str,
        schedule: SyncSchedule,
        action: str,
        old: dict[str, Any],
        new: dict[str, Any],
        changed_fields: dict[str, Any],
        actor_user_id: str | None,
        actor_role: str | None,
        actor_api_key_id: str | None,
    ) -> None:
        """Append a sanitised audit entry (old/new schedule, no secrets)."""
        detail = _sanitise_audit_detail(
            {
                "scope": schedule.scope,
                "target_id": schedule.target_id,
                "changed": changed_fields,
                "old_schedule": sanitise_schedule(old.get("schedule")),
                "old_timezone": old.get("timezone"),
                "new_schedule": sanitise_schedule(new.get("schedule")),
                "new_timezone": new.get("timezone"),
                "enabled": schedule.enabled,
            }
        )
        # Keep the payload lean (mirrors connection_audit._MAX_DETAIL_CHARS).
        import json

        encoded = json.dumps(detail, default=str)
        if len(encoded) > _MAX_DETAIL_CHARS:
            detail = {"truncated": True, "scope": schedule.scope}
        entry = ConnectionAuditLog(
            tenant_id=tenant_id,
            connection_id=(
                schedule.target_id
                if schedule.scope == SCOPE_INGESTION
                else None
            ),
            provider_key=schedule.scope,
            action=action,
            detail=detail,
            actor_user_id=actor_user_id or actor_api_key_id,
            actor_role=actor_role,
        )
        self._session.add(entry)
        await self._session.flush()


# ── Worker-facing helpers ─────────────────────────────────────────────


async def ensure_schedule_for_connection(
    session: AsyncSession,
    *,
    tenant_id: str,
    connection_id: str,
    provider_key: str,
    timezone: str | None = None,
) -> SyncSchedule | None:
    """Ensure a default schedule for an ingestion connection.

    Returns ``None`` for providers that are not schedulable (they keep
    their own triggers, e.g. DEGIRO watchfolders).
    """
    if provider_key not in INGESTION_SCHEDULABLE_PROVIDERS:
        return None
    svc = SyncScheduleService(session)
    return await svc.ensure_for_scope(
        tenant_id,
        scope=SCOPE_INGESTION,
        target_id=connection_id,
        timezone=timezone,
    )


async def ensure_schedule_for_exporter(
    session: AsyncSession,
    *,
    tenant_id: str,
    exporter_key: str,
    timezone: str | None = None,
) -> SyncSchedule | None:
    """Ensure a default schedule for an export target."""
    if exporter_key not in EXPORT_SCHEDULABLE_EXPORTERS:
        return None
    svc = SyncScheduleService(session)
    return await svc.ensure_for_scope(
        tenant_id,
        scope=SCOPE_EXPORT,
        target_id=exporter_key,
        timezone=timezone,
    )


def describe_schedule(schedule: SyncSchedule) -> str:
    """Human-readable Dutch summary for the UI (``Elke werkdag om 07:00``)."""
    return human_readable(schedule.schedule, timezone=schedule.timezone)
