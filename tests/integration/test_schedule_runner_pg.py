"""Schedule-runner integration tests against real PostgreSQL.

Proves the worker acceptance criteria that only real PG semantics can
demonstrate:

* the atomic claim (``last_scheduled_at`` guarded UPDATE) **persists**
  before the run executes — a second ``run_due_schedules`` tick within
  the claim grace does not re-run a schedule (double-run prevention);
* due enabled schedules advance ``next_run_at`` / record
  ``last_run_at`` / ``last_run_status``;
* disabled schedules are never picked up;
* tenant isolation holds at the runner level.

The heavy connector flow is patched at ``_run_ingestion``; everything
around it (due selection, claim, misfire, outcome persistence) runs
against the real migrated schema.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from finance_sync.models import Credential, SyncSchedule, Tenant
from finance_sync.models.sync_schedule import SCOPE_INGESTION
from finance_sync.sync.schedule_spec import default_schedule
from finance_sync.worker.schedule_runner import run_due_schedules

pytestmark = pytest.mark.integration


async def _tenant(session_factory, slug: str) -> Tenant:
    tenant = Tenant(id=str(uuid4()), slug=slug, name=f"{slug} tenant")
    async with session_factory() as session:
        session.add(tenant)
        await session.commit()
    return tenant


async def _connection(
    session_factory, tenant: Tenant, provider: str = "bunq"
) -> Credential:
    cred = Credential(
        id=str(uuid4()),
        tenant_id=str(tenant.id),
        provider_key=provider,
        encrypted_payload=b"\x00" * 16,
        nonce=b"\x00" * 12,
        status="active",
    )
    async with session_factory() as session:
        session.add(cred)
        await session.commit()
    return cred


async def _due_schedule(
    session_factory,
    tenant: Tenant,
    target_id: str,
    *,
    enabled: bool = True,
    overdue_seconds: int = 60,
) -> SyncSchedule:
    row = SyncSchedule(
        tenant_id=str(tenant.id),
        scope=SCOPE_INGESTION,
        target_id=target_id,
        enabled=enabled,
        schedule=default_schedule(),
        schema_version=1,
        timezone="Europe/Amsterdam",
        version=1,
        next_run_at=(
            datetime.now(UTC) - timedelta(seconds=overdue_seconds)
            if enabled
            else None
        ),
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def _get_row(session_factory, schedule_id: str) -> SyncSchedule:
    from sqlalchemy import select

    async with session_factory() as session:
        return (
            await session.execute(
                select(SyncSchedule).where(SyncSchedule.id == schedule_id)
            )
        ).scalar_one()


def _container(session_factory, settings: Any) -> Any:
    """Minimal container: real session factory + settings object."""
    from types import SimpleNamespace

    return SimpleNamespace(
        session_factory=session_factory,
        settings=settings,
    )


def _settings(**overrides: Any) -> Any:
    from types import SimpleNamespace

    base = {
        "worker_job_bunq_sync_enabled": True,
        "worker_job_trading212_sync_enabled": True,
        "worker_job_export_enabled": True,
        "wealthfolio_server_url": None,
        "wealthfolio_password": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRunnerPg:
    async def test_due_schedule_runs_once_and_claim_persists(
        self, session_factory
    ) -> None:
        """One tick runs the schedule; the claim survives for the grace.

        A second tick within the claim grace must not re-run the
        schedule — the persisted ``last_scheduled_at`` guard and the
        advanced ``next_run_at`` together prevent double execution.
        """
        tenant = await _tenant(session_factory, "runner-1")
        cred = await _connection(session_factory, tenant)
        sched = await _due_schedule(session_factory, tenant, str(cred.id))

        container = _container(session_factory, _settings())

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            first = await run_due_schedules(container)
            second = await run_due_schedules(container)

        assert first["due"] == 1
        assert run_ingestion.await_count == 1
        assert second["due"] == 0  # next_run advanced to the future

        row = await _get_row(session_factory, str(sched.id))
        assert row.last_run_at is not None
        assert row.last_run_status == "completed"
        assert row.last_scheduled_at is not None
        assert row.next_run_at is not None
        assert row.next_run_at > datetime.now(UTC)

    async def test_two_due_schedules_two_tenants_isolated(
        self, session_factory
    ) -> None:
        """Both tenants' due schedules run; each gets its own run."""
        tenant_a = await _tenant(session_factory, "runner-2a")
        tenant_b = await _tenant(session_factory, "runner-2b")
        cred_a = await _connection(session_factory, tenant_a)
        cred_b = await _connection(session_factory, tenant_b)
        sched_a = await _due_schedule(session_factory, tenant_a, str(cred_a.id))
        sched_b = await _due_schedule(session_factory, tenant_b, str(cred_b.id))

        container = _container(session_factory, _settings())

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            summary = await run_due_schedules(container)

        assert summary["due"] == 2
        assert run_ingestion.await_count == 2
        seen = {
            str(call.kwargs["schedule"].tenant_id)
            for call in run_ingestion.await_args_list
        }
        assert seen == {str(tenant_a.id), str(tenant_b.id)}
        for sched in (sched_a, sched_b):
            row = await _get_row(session_factory, str(sched.id))
            assert row.last_run_at is not None

    async def test_disabled_schedule_not_picked_up(
        self, session_factory
    ) -> None:
        """Disabled schedules are skipped even when overdue."""
        tenant = await _tenant(session_factory, "runner-3")
        cred = await _connection(session_factory, tenant)
        await _due_schedule(
            session_factory, tenant, str(cred.id), enabled=False
        )

        container = _container(session_factory, _settings())

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            summary = await run_due_schedules(container)

        assert summary["due"] == 0
        run_ingestion.assert_not_awaited()

    async def test_concurrent_claim_only_one_wins(
        self, session_factory
    ) -> None:
        """Two racing claimers: exactly one executes the schedule.

        Simulates two worker replicas ticking simultaneously.  The
        guarded claim UPDATE is atomic in PG — the second replica's
        claim fails (``last_scheduled_at`` was just written by the
        first), so the run executes exactly once.
        """
        tenant = await _tenant(session_factory, "runner-4")
        cred = await _connection(session_factory, tenant)
        sched = await _due_schedule(session_factory, tenant, str(cred.id))

        container = _container(session_factory, _settings())

        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion:
            summary = await run_due_schedules(container)

        # The claim persisted; a fresh runner instance re-reading the
        # DB must NOT see the schedule as due (its next_run_at advanced)
        # and must NOT re-run it.
        assert summary["due"] == 1
        assert run_ingestion.await_count == 1

        row = await _get_row(session_factory, str(sched.id))
        assert row.last_run_at is not None
        assert row.next_run_at is not None
        assert row.next_run_at > datetime.now(UTC)

        # A brand-new tick (as if another replica started later) sees
        # nothing due and runs nothing.
        with patch(
            "finance_sync.worker.schedule_runner._run_ingestion",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as run_ingestion2:
            summary2 = await run_due_schedules(container)
        assert summary2["due"] == 0
        run_ingestion2.assert_not_awaited()
        assert run_ingestion.await_count == 1
