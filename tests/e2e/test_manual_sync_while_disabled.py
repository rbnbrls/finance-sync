"""E2E — manual sync stays possible while a schedule is disabled.

Story acceptance criterion (backlog/configureerbare-syncschema-s-per-
connector-en-exporter.md):

    Een uitgeschakeld schema start geen nieuwe geplande runs; een
    handmatige sync/export blijft expliciet mogelijk en verandert het
    schema niet.

Proves the full stack against real PostgreSQL + Redis:

1. a due enabled schedule is picked up by the worker tick and executes
   exactly once through the real connector flow (SyncRun created);
2. disabling the schedule via the API sets ``enabled=false`` and clears
   ``next_run_at``, and records an audit entry with the actor;
3. a subsequent worker tick sees nothing due (disabled schedules are
   never picked up);
4. ``POST /sync/connections/{id}`` — the manual per-connection sync —
   still executes and completes, producing a new SyncRun;
5. the manual run does **not** mutate the schedule: still disabled,
   ``next_run_at`` still NULL, version unchanged after the disable;
6. re-enabling recomputes ``next_run_at``.

The runner/API unit+integration suites cover the pieces; only a
full-stack run proves the manual path and the schedule path coexist on
one connection against a real database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select

from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.models import (
    ConnectionAuditLog,
    Credential,
    SyncRun,
    SyncSchedule,
    Tenant,
    User,
)
from finance_sync.models.enums import UserRole
from finance_sync.models.sync_schedule import SCOPE_INGESTION
from finance_sync.services.auth import (
    create_access_token,
    encrypt_credential,
    hash_password,
)
from finance_sync.sync.schedule_spec import default_schedule
from finance_sync.worker.schedule_runner import run_due_schedules

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.config.settings import Settings
    from finance_sync.container import Container

pytestmark = pytest.mark.e2e

# Provider key registered for the mock connector below.
_E2E_PROVIDER = "mock_e2e"


# ── Minimal mock connector (same interception point as the existing
# ── exactly-once e2e suite: ConnectorRegistry.get_connector). ────────


class _StaticMockConnector(Connector):
    """Returns one fixed account and one fixed transaction."""

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        self.supported_resources: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        return _E2E_PROVIDER

    async def authenticate(self) -> None:
        return None

    async def fetch_accounts(self) -> list[RawAccount]:
        return [
            RawAccount(
                external_account_id="acc_manual_1",
                name="Manual Checking",
                account_type="checking",
                currency_code="EUR",
                current_balance=Decimal("100.00"),
            )
        ]

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        txn = RawTransaction(
            external_transaction_id="txn_manual_1",
            external_account_id="acc_manual_1",
            amount=Decimal("-12.50"),
            currency_code="EUR",
            occurred_at=datetime.now(UTC) - timedelta(days=1),
            description="Manual txn",
            transaction_type="payment",
            status="booked",
        )
        if account_id is not None and txn.external_account_id != account_id:
            return []
        return [txn]


def _mock_connector_factory(config: ConnectorConfig) -> _StaticMockConnector:
    return _StaticMockConnector(config)


@pytest.fixture
def mock_connector_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``ConnectorRegistry.get_connector`` to the static mock."""
    monkeypatch.setattr(
        ConnectorRegistry,
        "get_connector",
        staticmethod(_mock_connector_factory),
    )


@pytest.fixture
async def e2e_client(
    e2e_app: Any,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client against the in-process FastAPI app."""
    transport = httpx.ASGITransport(app=e2e_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://e2e"
    ) as client:
        yield client


@pytest.fixture
async def seeded_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    e2e_settings: Settings,
) -> dict[str, Any]:
    """Tenant + admin user + encrypted schedulable credential."""
    async with session_factory() as session:
        tenant = Tenant(slug="e2e-manual-sync", name="E2E Manual Sync")
        session.add(tenant)
        await session.flush()

        user = User(
            email="e2e-manual@finance-sync.local",
            tenant_id=str(tenant.id),
            hashed_password=hash_password("e2e-password"),
            display_name="E2E Admin",
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(user)

        ciphertext, nonce = encrypt_credential(
            json.dumps({"api_key": "e2e-key"}), e2e_settings
        )
        cred = Credential(
            tenant_id=str(tenant.id),
            provider_key=_E2E_PROVIDER,
            encrypted_payload=ciphertext,
            nonce=nonce,
            description="E2E manual credential",
            status="active",
        )
        session.add(cred)
        await session.commit()

        tenant_id = str(tenant.id)
        user_id = str(user.id)
        conn_id = str(cred.id)

    token = create_access_token(
        {"sub": user_id, "tenant_id": tenant_id, "role": "admin"},
        e2e_settings,
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "connection_id": conn_id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


# ── DB helpers ───────────────────────────────────────────────────────


async def _get_schedule(
    session_factory: async_sessionmaker[AsyncSession], schedule_id: str
) -> SyncSchedule:
    async with session_factory() as session:
        return (
            await session.execute(
                select(SyncSchedule).where(SyncSchedule.id == schedule_id)
            )
        ).scalar_one()


async def _count_runs(
    session_factory: async_sessionmaker[AsyncSession], connection_id: str
) -> int:
    async with session_factory() as session:
        return len(
            (
                await session.scalars(
                    select(SyncRun).where(
                        SyncRun.connection_id == connection_id
                    )
                )
            ).all()
        )


async def _audit_actions(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> list[str]:
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ConnectionAuditLog).where(
                    ConnectionAuditLog.tenant_id == tenant_id
                )
            )
        ).all()
    return [str(r.action) for r in rows]


class TestManualSyncWhileScheduleDisabled:
    async def test_manual_sync_works_with_disabled_schedule(
        self,
        e2e_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        e2e_container: Container,
        seeded_tenant: dict[str, Any],
        mock_connector_installed: None,
    ) -> None:
        headers = seeded_tenant["headers"]
        tenant_id = seeded_tenant["tenant_id"]
        conn_id = seeded_tenant["connection_id"]

        # ── 1. Enabled, due schedule → worker tick executes once ─────
        sched = SyncSchedule(
            tenant_id=tenant_id,
            scope=SCOPE_INGESTION,
            target_id=conn_id,
            enabled=True,
            schedule=default_schedule(),
            schema_version=1,
            timezone="Europe/Amsterdam",
            version=1,
            next_run_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        async with session_factory() as session:
            session.add(sched)
            await session.commit()
            await session.refresh(sched)
        schedule_id = str(sched.id)

        summary = await run_due_schedules(e2e_container)
        assert summary["due"] == 1
        assert summary["results"][0]["status"] == "completed"
        assert await _count_runs(session_factory, conn_id) == 1

        # ── 2. Disable via the API (audited, next_run_at cleared) ────
        resp = await e2e_client.post(
            f"/api/v1/sync-schedules/{schedule_id}/disable", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        assert body["enabled"] is False
        assert body["next_run_at"] is None

        row = await _get_schedule(session_factory, schedule_id)
        assert row.enabled is False
        assert row.next_run_at is None
        version_after_disable = row.version

        # The disable is audited with the acting user.
        assert "schedule.update" in await _audit_actions(
            session_factory, tenant_id
        )

        # ── 3. Worker tick: disabled → nothing due ───────────────────
        summary2 = await run_due_schedules(e2e_container)
        assert summary2["due"] == 0

        # ── 4. Manual per-connection sync still works ────────────────
        resp2 = await e2e_client.post(
            f"/api/v1/sync/connections/{conn_id}", headers=headers
        )
        assert resp2.status_code == 202, resp2.text
        manual = resp2.json()
        assert manual["status"] == "completed"
        assert manual["sync_run_id"] is not None
        assert await _count_runs(session_factory, conn_id) == 2

        # ── 5. The manual run did NOT mutate the schedule ─────────────
        after = await _get_schedule(session_factory, schedule_id)
        assert after.enabled is False
        assert after.next_run_at is None
        assert after.version == version_after_disable
        # Provenance stays worker-owned: the manual run neither advanced
        # the scheduled-run watermark nor recorded a schedule-level
        # outcome (last_scheduled_at/last_run_at are only ever written by
        # the worker's claim+run path, never by a manual sync).
        assert after.last_scheduled_at == row.last_scheduled_at
        assert after.last_run_at == row.last_run_at
        assert after.last_run_status == row.last_run_status

        # ── 6. Re-enable recomputes next_run_at (full-path check) ────
        resp3 = await e2e_client.post(
            f"/api/v1/sync-schedules/{schedule_id}/enable", headers=headers
        )
        assert resp3.status_code == 200, resp3.text
        assert resp3.json()["enabled"] is True
        assert resp3.json()["next_run_at"] is not None
