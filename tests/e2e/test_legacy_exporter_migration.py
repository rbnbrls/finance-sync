"""E2E tests — legacy exporter env-var settings migrate into destinations.

Covers the acceptance criterion that the old global exporter configuration
is migrated into one equivalent stored destination, remains idempotent, and
that the retired ``/exporters/*`` surface returns a clear 410 migration
error pointing at the destinations API.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from finance_sync.lifespan import _bootstrap_legacy_export_targets
from finance_sync.models import ExportTarget, SyncSchedule, Tenant
from finance_sync.models.export_target import TARGET_ACTIVE
from finance_sync.models.sync_schedule import SCOPE_EXPORT
from tests.e2e.destinations_helpers import (
    dest_client,
    seeded_destination_tenant,
)

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.e2e

client = dest_client
seeded_tenant = seeded_destination_tenant


def _legacy_settings(**overrides: Any) -> SimpleNamespace:
    """Settings that look like a deployment still using the old exporter env
    vars for Wealthfolio (Actual Budget left unset)."""
    values: dict[str, str] = {
        "wealthfolio_server_url": "http://192.168.1.71:5007",
        "wealthfolio_password": "wf-secret",
        "actual_budget_server_url": "",
        "actual_budget_password": "",
        "actual_budget_budget_name": "",
        "actual_budget_sync_id": "",
        "actual_budget_encryption_password": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _MiniContainer:
    """The subset of ``Container`` that the bootstrap reads.

    ``_bootstrap_legacy_export_targets`` only touches ``settings`` and
    ``session_factory``.
    """

    def __init__(
        self,
        settings: SimpleNamespace,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory


async def _ensure_default_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bootstrap migrates into the ``default`` tenant ({slug='default'})."""
    async with session_factory() as session:
        existing = await session.scalar(
            select(Tenant.id).where(Tenant.slug == "default")
        )
        if existing is None:
            session.add(Tenant(slug="default", name="Default Tenant"))
            await session.commit()


async def _export_targets(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[ExportTarget]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(ExportTarget))).scalars().all()
        )


class TestLegacyExporterMigration:
    """Old env vars become idempotent active destinations."""

    async def test_bootstrap_creates_active_encrypted_destination(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _ensure_default_tenant(session_factory)
        container = _MiniContainer(_legacy_settings(), session_factory)

        await _bootstrap_legacy_export_targets(container)

        targets = await _export_targets(session_factory)
        assert len(targets) == 1
        migrated = targets[0]
        assert migrated.target_type == "wealthfolio"
        assert migrated.status == TARGET_ACTIVE
        assert migrated.schedule_id is not None  # wired to an export schedule
        # The secret is envelope-encrypted; never stored in plain config.
        assert migrated.encrypted_secret is not None
        assert "wf-secret" not in (migrated.configuration or {})

    async def test_bootstrap_is_idempotent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _ensure_default_tenant(session_factory)
        container = _MiniContainer(_legacy_settings(), session_factory)

        await _bootstrap_legacy_export_targets(container)
        await _bootstrap_legacy_export_targets(container)

        targets = await _export_targets(session_factory)
        assert len(targets) == 1  # never a duplicate migration
        async with session_factory() as session:
            schedules = await session.scalar(
                select(func.count())
                .select_from(SyncSchedule)
                .where(SyncSchedule.scope == SCOPE_EXPORT)
            )
        assert schedules == 1

    async def test_retired_exporters_surface_returns_410(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        response = await client.get("/api/v1/exporters/types", headers=headers)
        assert response.status_code == 410
        detail = str(response.json()["detail"])
        assert "/api/v1/destinations" in detail
