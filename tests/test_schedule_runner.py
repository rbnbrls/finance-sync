"""Worker tests for the tenant-scoped schedule runner (schedule_runner.py).

Covers the worker acceptance criteria of the story:

* due enabled schedules are claimed and executed exactly once per
  window (the atomic claim persists across the run — a second tick
  within the claim grace does not re-run);
* disabled schedules are never picked up;
* tenant isolation: a schedule only runs for its own connection and
  never touches another tenant's rows;
* the global ``WORKER_JOB_*`` flags remain operational gates (a
  disabled gate skips, it does not run);
* misfires older than the catch-up window are reset, not run;
* outcome (last_run_at / status / next_run_at advance) is persisted.

The heavy connector/exporter flows are patched at the
``_run_ingestion`` / ``_run_export`` boundaries; the scheduling
mechanics (claim, due selection, misfire, outcome persistence) run
against the real aiosqlite-backed session machinery so the tests prove
the actual SQL paths, including the claim-commit idempotency.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Make JSONB work with SQLite (same pattern as test_reconciliation_integration).
if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[assignment]

# Also make the Uuid bind processor accept strings (not just UUID objects).
import uuid as _uuid_mod

from sqlalchemy import types as _sa_types

_uuid_bind_orig = _sa_types.Uuid.bind_processor


def _uuid_bind_patched(self, dialect):
    proc = _uuid_bind_orig(self, dialect)
    if proc is None or not self.as_uuid:
        return proc

    def _patched(value):
        if value is not None:
            if isinstance(value, str):
                return _uuid_mod.UUID(value).hex
            return value.hex
        return value

    return _patched


_sa_types.Uuid.bind_processor = _uuid_bind_patched

from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.db import Base
from finance_sync.models import Credential, SyncSchedule, Tenant
from finance_sync.models.sync_schedule import SCOPE_INGESTION
from finance_sync.sync.schedule_spec import default_schedule
from finance_sync.worker.schedule_runner import (
    CATCHUP_MAX_DELAY,
    run_due_schedules,
    run_export,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.asyncio


def _make_settings(**overrides: Any) -> Settings:
    from pydantic import SecretStr

    base: dict[str, Any] = {
        "database_url": None,
        "redis_url": None,
        "secret_key": SecretStr("test-secret-key-at-least-16-chars"),
        "worker_job_bunq_sync_enabled": True,
        "worker_job_trading212_sync_enabled": True,
        "worker_job_export_enabled": True,
        "worker_job_schedules_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


@pytest.fixture
def engine() -> Generator[AsyncEngine, None, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)

    async def _create_all() -> None:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_all())
    yield eng
    asyncio.run(eng.dispose())


@pytest.fixture
def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


def _make_container(
    session_factory: async_sessionmaker[AsyncSession],
    **settings_overrides: Any,
) -> Container:
    container = Container()
    container._settings = _make_settings(**settings_overrides)
    container._session_factory = session_factory
    return container


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    rows: list[Any],
) -> None:
    async with session_factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()
        for row in rows:
            await session.refresh(row)


def _tenant(slug: str) -> Tenant:
    return Tenant(id=str(uuid4()), slug=slug, name=f"{slug} tenant")


def _connection(tenant: Tenant, provider: str = "bunq") -> Credential:
    return Credential(
        id=str(uuid4()),
        tenant_id=str(tenant.id),
        provider_key=provider,
        encrypted_payload=b"\x00" * 16,
        nonce=b"\x00" * 12,
        status="active",
    )


def _schedule(
    tenant: Tenant,
    target_id: str,
    *,
    scope: str = SCOPE_INGESTION,
    enabled: bool = True,
    due_seconds_ago: int = 60,
) -> SyncSchedule:
    row = SyncSchedule(
        tenant_id=str(tenant.id),
        scope=scope,
        target_id=target_id,
        enabled=enabled,
        schedule=default_schedule(),
        schema_version=1,
        timezone="Europe/Amsterdam",
        version=1,
    )
    if enabled:
        # Force a due window: next_run_at in the past (but inside the
        # catch-up window so the runner executes it instead of resetting).
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=due_seconds_ago)
    # Disabled schedules carry no next run (matches service semantics).
    return row


async def _get_row(
    session_factory: async_sessionmaker[AsyncSession],
    schedule_id: str,
) -> SyncSchedule:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(SyncSchedule).where(SyncSchedule.id == schedule_id)
            )
        ).scalar_one()
    # SQLite returns naive DATETIME; normalise for aware comparisons.
    row.next_run_at = _ensure_aware_test(row.next_run_at)
    row.last_run_at = _ensure_aware_test(row.last_run_at)
    row.last_scheduled_at = _ensure_aware_test(row.last_scheduled_at)
    return row


def _ensure_aware_test(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class TestRunDueSchedules:
    async def test_export_skips_when_same_target_is_already_running(
        self, session_factory
    ) -> None:
        """A second worker cannot overlap a destination export."""
        tenant = _tenant("t1")
        schedule = _schedule(tenant, "wealthfolio:target-1")
        await _seed(session_factory, [tenant, schedule])
        container = _make_container(
            session_factory,
            redis_url="redis://test",
        )

        class BusyRedis:
            async def set(self, *_args: Any, **_kwargs: Any) -> bool:
                return False

        container._redis = BusyRedis()
        with patch(
            "finance_sync.worker.schedule_runner._run_export_unlocked",
            new=AsyncMock(),
        ) as run_unlocked:
            outcome = await run_export(container, schedule=schedule)

        assert outcome == {"status": "skipped", "reason": "export_in_progress"}
        run_unlocked.assert_not_awaited()

    async def test_due_schedule_runs_once_and_advances(
        self, session_factory
    ) -> None:
        """A due enabled schedule runs exactly once; next_run advances."""
        tenant = _tenant("t1")
        cred = _connection(tenant)
        sched = _schedule(tenant, str(cred.id))
        await _seed(session_factory, [tenant, cred, sched])

        container = _make_container(session_factory)

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            summary = await run_due_schedules(container)

        assert summary["due"] == 1
        run_ingestion.assert_awaited_once()
        # Outcome persisted + next run advanced to the future.
        row = await _get_row(session_factory, str(sched.id))
        assert row.last_run_at is not None
        assert row.last_run_status == "completed"
        assert row.next_run_at is not None
        assert row.next_run_at > datetime.now(UTC)
        assert row.last_scheduled_at is not None

    async def test_second_tick_within_grace_does_not_double_run(
        self, session_factory
    ) -> None:
        """The persisted claim prevents a second tick from re-running."""
        tenant = _tenant("t1")
        cred = _connection(tenant)
        sched = _schedule(tenant, str(cred.id))
        await _seed(session_factory, [tenant, cred, sched])

        container = _make_container(session_factory)

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            first = await run_due_schedules(container)
            second = await run_due_schedules(container)

        # First tick ran it; the second tick must not re-run (the claim
        # persisted and next_run_at moved to the future).
        assert first["due"] == 1
        assert run_ingestion.await_count == 1
        # Second tick saw either zero due (next_run advanced) or the
        # already-claimed skip; either way no second execution.
        assert second["due"] <= 1
        assert run_ingestion.await_count == 1

    async def test_disabled_schedule_not_picked_up(
        self, session_factory
    ) -> None:
        """Disabled schedules are never selected, never executed."""
        tenant = _tenant("t1")
        cred = _connection(tenant)
        sched = _schedule(tenant, str(cred.id), enabled=False)
        await _seed(session_factory, [tenant, cred, sched])

        container = _make_container(session_factory)

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            summary = await run_due_schedules(container)

        assert summary["due"] == 0
        run_ingestion.assert_not_awaited()
        row = await _get_row(session_factory, str(sched.id))
        assert row.last_run_at is None
        assert row.next_run_at is None  # disabled → no next run

    async def test_tenant_isolation(self, session_factory) -> None:
        """Each schedule runs only its own tenant's connection."""
        tenant_a = _tenant("t-a")
        tenant_b = _tenant("t-b")
        cred_a = _connection(tenant_a)
        cred_b = _connection(tenant_b)
        sched_a = _schedule(tenant_a, str(cred_a.id))
        sched_b = _schedule(tenant_b, str(cred_b.id))
        await _seed(
            session_factory,
            [tenant_a, tenant_b, cred_a, cred_b, sched_a, sched_b],
        )

        container = _make_container(session_factory)

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(
                side_effect=lambda _c, *, schedule: {
                    "status": "completed",
                    "tenant": str(schedule.tenant_id),
                }
            ),
        ) as run_ingestion:
            summary = await run_due_schedules(container)

        assert summary["due"] == 2
        assert run_ingestion.await_count == 2
        # Each run saw the correct schedule → correct tenant/connection.
        seen_tenants = {
            str(call.kwargs["schedule"].tenant_id)
            for call in run_ingestion.await_args_list
        }
        assert seen_tenants == {str(tenant_a.id), str(tenant_b.id)}
        # Both rows advanced.
        row_a = await _get_row(session_factory, str(sched_a.id))
        row_b = await _get_row(session_factory, str(sched_b.id))
        assert row_a.last_run_at is not None
        assert row_b.last_run_at is not None

    async def test_failing_connection_does_not_block_siblings(
        self, session_factory
    ) -> None:
        """A failing run is recorded and sibling schedules still run."""
        tenant = _tenant("t1")
        cred_ok = _connection(tenant)
        cred_fail = _connection(tenant)
        sched_ok = _schedule(tenant, str(cred_ok.id))
        sched_fail = _schedule(tenant, str(cred_fail.id))
        await _seed(
            session_factory,
            [tenant, cred_ok, cred_fail, sched_ok, sched_fail],
        )

        container = _make_container(session_factory)

        async def _fake_run(_c, *, schedule):
            if schedule.target_id == str(cred_fail.id):
                provider_down = "provider down"
                raise RuntimeError(provider_down)
            return {"status": "completed"}

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(side_effect=_fake_run),
        ):
            summary = await run_due_schedules(container)

        statuses = {r["target"]: r["status"] for r in summary["results"]}
        assert statuses[str(cred_fail.id)] == "failed"
        assert statuses[str(cred_ok.id)] == "completed"
        row_ok = await _get_row(session_factory, str(sched_ok.id))
        row_fail = await _get_row(session_factory, str(sched_fail.id))
        assert row_ok.last_run_status == "completed"
        assert row_fail.last_run_status == "failed"
        assert row_fail.last_run_error is not None

    async def test_misfire_older_than_catchup_window_is_reset(
        self, session_factory
    ) -> None:
        """A schedule > CATCHUP_MAX_DELAY overdue is reset, not run."""
        tenant = _tenant("t1")
        cred = _connection(tenant)
        sched = _schedule(
            tenant,
            str(cred.id),
            due_seconds_ago=int(CATCHUP_MAX_DELAY.total_seconds()) + 3600,
        )
        await _seed(session_factory, [tenant, cred, sched])

        container = _make_container(session_factory)

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            summary = await run_due_schedules(container)

        assert run_ingestion.assert_not_awaited() is None
        run_ingestion.assert_not_awaited()
        assert summary["results"][0]["reason"] == "misfire_reset"
        row = await _get_row(session_factory, str(sched.id))
        assert row.last_run_at is None
        # next_run_at reset to a future instant.
        assert row.next_run_at is not None
        assert row.next_run_at > datetime.now(UTC)

    async def test_global_gate_disabled_skips_ingestion(
        self, session_factory
    ) -> None:
        """A disabled WORKER_JOB_BUNQ_SYNC_ENABLED gate skips (no run)."""
        tenant = _tenant("t1")
        cred = _connection(tenant)
        sched = _schedule(tenant, str(cred.id))
        await _seed(session_factory, [tenant, cred, sched])

        container = _make_container(
            session_factory, worker_job_bunq_sync_enabled=False
        )

        # The real _run_ingestion resolves the connection, checks the
        # operational gate and returns a skip without executing.
        from finance_sync.worker.schedule_runner import _run_ingestion

        outcome = await _run_ingestion(container, schedule=sched)

        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "global_gate_disabled"
        # The schedule row was not marked as run.
        row = await _get_row(session_factory, str(sched.id))
        assert row.last_run_at is None
        assert row.last_run_status is None

    async def test_paused_connection_skipped_by_ingestion(
        self, session_factory
    ) -> None:
        """A paused connection is skipped by the scheduled runner."""
        tenant = _tenant("t1")
        cred = _connection(tenant, provider="trading212")
        cred.status = "paused"
        sched = _schedule(tenant, str(cred.id))
        await _seed(session_factory, [tenant, cred, sched])

        container = _make_container(session_factory)

        from finance_sync.worker.schedule_runner import _run_ingestion

        outcome = await _run_ingestion(container, schedule=sched)

        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "paused"
        row = await _get_row(session_factory, str(sched.id))
        assert row.last_run_at is None
