"""Sync-schedule service integration tests against real PostgreSQL.

Covers the data-layer acceptance criteria that depend on real PG
semantics (the aiosqlite unit suite cannot prove them):

* ``SyncScheduleService.ensure_for_scope`` seeds an enabled default
  schedule atomically for a new active connection / export target
  (same transaction — a rollback removes the schedule row too);
* the unique ``(tenant_id, scope, target_id)`` constraint is enforced
  at the DB level (parallel/duplicate creation converges to one row);
* ``resolve_tenant_timezone`` falls back to the documented default when
  no tenant timezone is available;
* schedule rows never carry credential/provider payload fields.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from finance_sync.db.uow import UnitOfWork
from finance_sync.models import Credential, SyncSchedule, Tenant
from finance_sync.models.sync_schedule import (
    DEFAULT_TIMEZONE,
    FALLBACK_TIMEZONE,
    SCOPE_EXPORT,
    SCOPE_INGESTION,
)
from finance_sync.services.sync_schedule import (
    ensure_schedule_for_connection,
    ensure_schedule_for_exporter,
    resolve_tenant_timezone,
)

pytestmark = pytest.mark.integration


async def _create_tenant(
    session_factory,
    *,
    slug: str,
) -> Tenant:
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.tenants.add(Tenant(slug=slug, name=f"{slug} tenant"))


async def _create_connection(
    session_factory,
    tenant: Tenant,
    *,
    provider_key: str = "bunq",
    status: str = "active",
) -> Credential:
    async with session_factory() as session:
        cred = Credential(
            tenant_id=str(tenant.id),
            provider_key=provider_key,
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status=status,
        )
        session.add(cred)
        await session.commit()
        await session.refresh(cred)
        return cred


class TestEnsureForScope:
    async def test_seeds_enabled_default_for_connection(
        self, session_factory
    ) -> None:
        tenant = await _create_tenant(session_factory, slug="seed-svc-1")
        cred = await _create_connection(session_factory, tenant)

        async with session_factory() as session:
            row = await ensure_schedule_for_connection(
                session,
                tenant_id=str(tenant.id),
                connection_id=str(cred.id),
                provider_key="bunq",
            )

        assert row is not None
        assert row.tenant_id == str(tenant.id)
        assert row.scope == SCOPE_INGESTION
        assert row.target_id == str(cred.id)
        assert row.enabled is True
        assert row.schema_version == 1
        assert row.timezone == DEFAULT_TIMEZONE
        assert row.schedule["frequency"] == "weekdays"
        assert row.schedule["time"] == "07:00"
        assert row.schedule["weekdays"] == [0, 1, 2, 3, 4]
        # next_run_at is computed (UTC, aware).
        assert row.next_run_at is not None
        assert row.next_run_at.tzinfo is not None
        assert row.next_run_at > datetime.now(UTC)

    async def test_seeds_export_schedule(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory, slug="seed-svc-exp")
        async with session_factory() as session:
            row = await ensure_schedule_for_exporter(
                session,
                tenant_id=str(tenant.id),
                exporter_key="wealthfolio",
            )
        assert row is not None
        assert row.scope == SCOPE_EXPORT
        assert row.target_id == "wealthfolio"
        assert row.enabled is True

    async def test_non_schedulable_provider_returns_none(
        self, session_factory
    ) -> None:
        tenant = await _create_tenant(session_factory, slug="seed-svc-nonsched")
        cred = await _create_connection(
            session_factory, tenant, provider_key="degiro_pension"
        )
        async with session_factory() as session:
            row = await ensure_schedule_for_connection(
                session,
                tenant_id=str(tenant.id),
                connection_id=str(cred.id),
                provider_key="degiro_pension",
            )
        assert row is None

    async def test_ensure_is_idempotent(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory, slug="seed-svc-idem")
        cred = await _create_connection(session_factory, tenant)

        async with session_factory() as session:
            first = await ensure_schedule_for_connection(
                session,
                tenant_id=str(tenant.id),
                connection_id=str(cred.id),
                provider_key="bunq",
            )
            await session.commit()
        async with session_factory() as session:
            second = await ensure_schedule_for_connection(
                session,
                tenant_id=str(tenant.id),
                connection_id=str(cred.id),
                provider_key="bunq",
            )
            await session.commit()

        assert first is not None and second is not None
        assert str(first.id) == str(second.id)
        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(SyncSchedule).where(
                        SyncSchedule.tenant_id == str(tenant.id),
                        SyncSchedule.scope == SCOPE_INGESTION,
                        SyncSchedule.target_id == str(cred.id),
                    )
                )
            ).all()
        assert len(rows) == 1

    async def test_rollback_removes_schedule_row(self, session_factory) -> None:
        """Atomicity: a rolled-back transaction removes the schedule too."""
        tenant = await _create_tenant(session_factory, slug="seed-svc-rb")

        async with session_factory() as session:
            with pytest.raises(RuntimeError):
                async with session.begin():
                    cred = Credential(
                        tenant_id=str(tenant.id),
                        provider_key="bunq",
                        encrypted_payload=b"\x00" * 16,
                        nonce=b"\x00" * 12,
                        status="active",
                    )
                    session.add(cred)
                    await session.flush()
                    await ensure_schedule_for_connection(
                        session,
                        tenant_id=str(tenant.id),
                        connection_id=str(cred.id),
                        provider_key="bunq",
                    )
                    await session.flush()
                    _msg = "boom"
                    raise RuntimeError(_msg)

        async with session_factory() as session:
            creds = (
                await session.scalars(
                    select(Credential).where(
                        Credential.tenant_id == str(tenant.id)
                    )
                )
            ).all()
            scheds = (
                await session.scalars(
                    select(SyncSchedule).where(
                        SyncSchedule.tenant_id == str(tenant.id)
                    )
                )
            ).all()
        assert creds == []
        assert scheds == []

    async def test_unique_constraint_enforced(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory, slug="seed-svc-uniq")
        cred = await _create_connection(session_factory, tenant)

        async with session_factory() as session:
            await ensure_schedule_for_connection(
                session,
                tenant_id=str(tenant.id),
                connection_id=str(cred.id),
                provider_key="bunq",
            )
            await session.commit()

        # A direct duplicate insert must violate the unique constraint.
        async with session_factory() as session:
            dup = SyncSchedule(
                tenant_id=str(tenant.id),
                scope=SCOPE_INGESTION,
                target_id=str(cred.id),
                enabled=True,
                schedule={"frequency": "daily", "time": "08:00"},
                schema_version=1,
                timezone="UTC",
            )
            session.add(dup)
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_no_secrets_in_rows(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory, slug="seed-svc-secret")
        cred = await _create_connection(session_factory, tenant)
        async with session_factory() as session:
            row = await ensure_schedule_for_connection(
                session,
                tenant_id=str(tenant.id),
                connection_id=str(cred.id),
                provider_key="bunq",
            )
        # The row has no credential/payload columns at all.
        assert row is not None
        assert not hasattr(row, "encrypted_payload")
        assert not hasattr(row, "nonce")
        assert not hasattr(row, "credentials")
        serialised = str(dict(row.schedule or {})).lower()
        for secret in ("token", "password", "api_key", "secret"):
            assert secret not in serialised


class TestResolveTenantTimezone:
    def test_uses_documented_default_when_tenant_tz_missing(self) -> None:
        assert resolve_tenant_timezone(None) == DEFAULT_TIMEZONE

    def test_prefers_provided_zone(self) -> None:
        assert resolve_tenant_timezone("America/New_York") == (
            "America/New_York"
        )

    def test_invalid_zone_falls_back(self) -> None:
        assert resolve_tenant_timezone("Mars/Olympus") == DEFAULT_TIMEZONE

    def test_utc_is_accepted(self) -> None:
        assert resolve_tenant_timezone("UTC") == "UTC"

    def test_fallback_chain_never_returns_none(self) -> None:
        assert resolve_tenant_timezone(None, fallback="Bogus/Zone") == (
            FALLBACK_TIMEZONE
        )
