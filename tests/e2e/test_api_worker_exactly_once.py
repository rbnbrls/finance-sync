"""E2E tests — API → worker exactly-once under at-least-once delivery.

Gap G-10 (roadmap IDs tc.4, ms.2.ac.1).  These tests prove the end-to-end
pipeline — FastAPI sync endpoint → transactional outbox → background
worker — yields an **exactly-once observable outcome** even when the same
work is delivered more than once (at-least-once semantics).

The at-least-once → exactly-once contract (asserted here and documented
in README.md):

* **Ingestion is exactly-once.**  Re-running a sync with identical
  provider data upserts by ``(tenant, provider, external_id)`` instead of
  inserting duplicates: accounts and transactions never appear twice.
* **Outbox is exactly-once.**  ``created`` events are emitted only when an
  entity is first persisted; re-syncs with unchanged data emit nothing.
  Every message carries a unique ``idempotency_key`` (DB unique
  constraint), so even a replayed event cannot create a second outbox
  row.
* **The worker is exactly-once at the outcome level.**  A processed
  message is transitioned ``pending → sent`` in the same committed
  transaction as its handler side effect.  A crash *between* delivery and
  the status commit leaves the message pending (the classic at-least-once
  window) and it is redelivered — but the redelivery only re-runs the
  handler; it never re-creates transactions, outbox entries or export
  runs.
* **Webhook fan-out is at-least-once by design.**  Each redelivered
  message triggers one more HTTP POST (consumers dedupe via
  ``event_id``/``idempotency_key``); the *domain state* stays
  exactly-once.  This mirrors the documented delivery semantics of the
  outbox → webhook pipeline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import sqlalchemy as sa
from aiohttp import web
from sqlalchemy import func, select, update

from finance_sync.api.v1.sync import SyncTriggerResponse
from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.db.uow import UnitOfWork
from finance_sync.models import (
    Account,
    Credential,
    OutboxMessage,
    SyncRun,
    Tenant,
    Transaction,
    User,
    Webhook,
    WebhookDeliveryLog,
)
from finance_sync.models.enums import (
    OutboxMessageStatus,
    UserRole,
)
from finance_sync.services.auth import (
    create_access_token,
    encrypt_credential,
    hash_password,
)
from finance_sync.worker.jobs import process_outbox_job

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.config.settings import Settings
    from finance_sync.container import Container

pytestmark = pytest.mark.e2e

# Connector key used for the whole suite (registered as a mock below).
E2E_PROVIDER = "mock_e2e"
# Observable fixtures returned by the mock connector on every sync.
E2E_ACCOUNT_ID = "acc_e2e_12345"
E2E_TXN_IDS = ["txn_e2e_1", "txn_e2e_2", "txn_e2e_3"]

# Webhook event types the capture webhook subscribes to — the full set of
# events the sync pipeline can emit.
E2E_WEBHOOK_EVENTS = [
    "account.created",
    "account.updated",
    "transaction.created",
    "transaction.updated",
]


# ── Mock connector ───────────────────────────────────────────────────


class StaticMockConnector(Connector):
    """A Connector subclass returning fixed accounts/transactions."""

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        accounts: list[RawAccount] | None = None,
        transactions: list[RawTransaction] | None = None,
    ) -> None:
        super().__init__(config)
        self._accounts = accounts or []
        self._transactions = transactions or []

    @property
    def name(self) -> str:
        return self.config.provider_type

    async def authenticate(self) -> None:
        return None

    async def fetch_accounts(self) -> list[RawAccount]:
        return self._accounts

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        filtered = self._transactions
        if account_id:
            filtered = [
                t for t in filtered if t.external_account_id == account_id
            ]
        if limit and limit < len(filtered):
            filtered = filtered[:limit]
        return [t for t in filtered if t.occurred_at >= since]


def _make_account() -> RawAccount:
    return RawAccount(
        external_account_id=E2E_ACCOUNT_ID,
        name="E2E Checking",
        account_type="checking",
        account_subtype=None,
        currency_code="EUR",
        current_balance=Decimal("1520.45"),
        available_balance=Decimal("1480.00"),
        iso_currency_code="EUR",
        provider_metadata={"iban": "NL00BANK0123456789"},
    )


def _make_transactions() -> list[RawTransaction]:
    """Three transactions dated a few days ago (always inside the
    orchestrator's 90-day default window, regardless of wall-clock)."""
    now = datetime.now(UTC)
    return [
        RawTransaction(
            external_transaction_id=txn_id,
            external_account_id=E2E_ACCOUNT_ID,
            amount=Decimal(f"-{10 + i}.50"),
            currency_code="EUR",
            occurred_at=now - timedelta(days=2 + i, hours=1),
            booked_at=now - timedelta(days=2 + i),
            description=f"E2E purchase {i}",
            transaction_type="purchase",
            status="booked",
            provider_fingerprint=f"e2e_fp_{i}",
        )
        for i, txn_id in enumerate(E2E_TXN_IDS)
    ]


def _mock_connector_factory(config: ConnectorConfig) -> StaticMockConnector:
    """Factory injected into ``ConnectorRegistry.get_connector``."""
    return StaticMockConnector(
        config,
        accounts=[_make_account()],
        transactions=_make_transactions(),
    )


# ── Local delivery capture server ────────────────────────────────────


class DeliveryCapture:
    """Local HTTP endpoint that records webhook deliveries in-process."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        if self.port is None:
            msg = "capture server not started"
            raise RuntimeError(msg)
        return f"http://127.0.0.1:{self.port}/hook"

    async def start(self) -> None:
        app = web.Application()

        async def _handler(request: web.Request) -> web.Response:
            body = await request.json()
            self.requests.append(body)
            return web.Response(status=200)

        app.router.add_post("/hook", _handler)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        # The runner records the bound (host, port) after site.start().
        self.port = int(self._runner.addresses[0][1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def event_ids(self) -> list[str]:
        return [str(r["event_id"]) for r in self.requests]


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def delivery_capture() -> AsyncGenerator[DeliveryCapture, None]:
    capture = DeliveryCapture()
    await capture.start()
    try:
        yield capture
    finally:
        await capture.stop()


@pytest.fixture
def mock_connector_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``ConnectorRegistry.get_connector`` to the static mock.

    The sync endpoint builds a fresh ``ConnectorRegistry()`` per request,
    so the class-level method is patched — the same interception point a
    real entry-point-registered connector would use.
    """
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
    delivery_capture: DeliveryCapture,
) -> dict[str, Any]:
    """Tenant + admin user + encrypted credential + subscribed webhook.

    Returns auth headers plus the ids the assertions need.
    """
    async with session_factory() as session:
        async with UnitOfWork(session) as uow:
            tenant = await uow.tenants.add(
                Tenant(slug="e2e-tenant", name="E2E Tenant")
            )
            user = User(
                email="e2e@finance-sync.local",
                tenant_id=str(tenant.id),
                hashed_password=hash_password("e2e-password"),
                display_name="E2E Admin",
                role=UserRole.ADMIN,
                is_active=True,
            )
            uow.session.add(user)

            ciphertext, nonce = encrypt_credential(
                json.dumps({"api_key": "e2e-key"}), e2e_settings
            )
            uow.session.add(
                Credential(
                    tenant_id=str(tenant.id),
                    provider_key=E2E_PROVIDER,
                    encrypted_payload=ciphertext,
                    nonce=nonce,
                    description="E2E mock credential",
                )
            )

            uow.session.add(
                Webhook(
                    tenant_id=str(tenant.id),
                    url=delivery_capture.url,
                    secret="e2e-webhook-secret",
                    events=E2E_WEBHOOK_EVENTS,
                    description="E2E capture webhook",
                    is_active=True,
                    rate_limit_max_per_minute=1000,
                )
            )

        tenant_id = str(tenant.id)
        user_id = str(user.id)

    token = create_access_token(
        {"sub": user_id, "tenant_id": tenant_id, "role": "admin"},
        e2e_settings,
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


# ── Assertion helpers ────────────────────────────────────────────────


async def _domain_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Row counts of the exactly-once-observable domain tables."""
    async with session_factory() as session:
        result: dict[str, int] = {}
        for label, model in (
            ("accounts", Account),
            ("transactions", Transaction),
            ("outbox", OutboxMessage),
            ("sync_runs", SyncRun),
            ("webhook_deliveries", WebhookDeliveryLog),
            ("export_runs", None),
        ):
            if model is None:
                # export_runs lives in the schema; assert it stays empty
                # (no spurious export activity from redeliveries).
                stmt = sa.text("SELECT count(*) FROM export_runs")
            else:
                stmt = select(func.count()).select_from(model)
            result[label] = (await session.execute(stmt)).scalar_one()
        return result


async def _unique_transaction_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[str]:
    async with session_factory() as session:
        rows = (
            (await session.execute(select(Transaction.external_transaction_id)))
            .scalars()
            .all()
        )
        return set(rows)


async def _outbox_idempotency_keys(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[str]:
    async with session_factory() as session:
        rows = (
            (await session.execute(select(OutboxMessage.idempotency_key)))
            .scalars()
            .all()
        )
        return {k for k in rows if k is not None}


async def _outbox_statuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(OutboxMessage.status)))
            .scalars()
            .all()
        )


# ── Tests ────────────────────────────────────────────────────────────


class TestApiWorkerExactlyOnce:
    """The API → worker flow is exactly-once under redelivery."""

    async def test_api_sync_and_worker_redelivery_is_exactly_once(
        self,
        e2e_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        e2e_container: Container,
        seeded_tenant: dict[str, str],
        delivery_capture: DeliveryCapture,
        mock_connector_installed: None,
    ) -> None:
        headers = seeded_tenant["headers"]

        # ── 1. Drive a sync via the API ─────────────────────────────
        response = await e2e_client.post(
            f"/api/v1/sync/{E2E_PROVIDER}", headers=headers
        )
        assert response.status_code == 202
        body: SyncTriggerResponse = SyncTriggerResponse.model_validate(
            response.json()
        )
        assert body.sync_runs[0].provider == E2E_PROVIDER
        assert body.sync_runs[0].status == "completed"
        assert body.sync_runs[0].transactions_synced == 3

        counts_after_api = await _domain_counts(session_factory)
        assert counts_after_api["accounts"] == 1
        assert counts_after_api["transactions"] == 3
        assert counts_after_api["outbox"] == 4  # 1 account + 3 transactions
        assert counts_after_api["sync_runs"] == 1
        assert counts_after_api["webhook_deliveries"] == 0
        assert counts_after_api["export_runs"] == 0

        # ── 2. Worker consumes the outbox (first delivery) ──────────
        tick1 = await process_outbox_job(e2e_container)
        assert tick1["processed"] == 4
        assert len(delivery_capture.requests) == 4
        assert len(set(delivery_capture.event_ids())) == 4  # one per message

        # ── 3. Redelivery: the same sync is driven again via the API
        #       (at-least-once — e.g. a worker re-running the job) ───
        response2 = await e2e_client.post(
            f"/api/v1/sync/{E2E_PROVIDER}", headers=headers
        )
        assert response2.status_code == 202

        tick2 = await process_outbox_job(e2e_container)
        assert tick2["processed"] == 0  # nothing left to deliver

        # ── 4. The observable outcome is exactly-once ───────────────
        counts_final = await _domain_counts(session_factory)
        # No duplicate domain rows: ingestion upserted, outbox untouched.
        assert counts_final["accounts"] == 1
        assert counts_final["transactions"] == 3
        assert counts_final["outbox"] == 4
        assert counts_final["webhook_deliveries"] == 4
        assert counts_final["export_runs"] == 0
        # The second sync is a *new attempt* (its own SyncRun row) but
        # produced zero new outcomes — attempts are not duplicates.
        assert counts_final["sync_runs"] == 2

        # Exactly-once identity sets: no duplicated external ids or
        # idempotency keys after the redelivered sync.
        assert await _unique_transaction_ids(session_factory) == set(
            E2E_TXN_IDS
        )
        keys = await _outbox_idempotency_keys(session_factory)
        assert len(keys) == 4
        assert len({k.split(":")[0] for k in keys}) == 2  # account+transaction

        # No message was delivered twice: the worker never re-fetched a
        # sent message.
        assert len(delivery_capture.requests) == 4

    async def test_worker_crash_before_ack_redelivers_without_duplicating(
        self,
        e2e_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        e2e_container: Container,
        seeded_tenant: dict[str, str],
        delivery_capture: DeliveryCapture,
        mock_connector_installed: None,
    ) -> None:
        headers = seeded_tenant["headers"]

        # ── 1. Sync via API, worker delivers everything ─────────────
        response = await e2e_client.post(
            f"/api/v1/sync/{E2E_PROVIDER}", headers=headers
        )
        assert response.status_code == 202

        tick1 = await process_outbox_job(e2e_container)
        assert tick1["processed"] == 4
        assert len(delivery_capture.requests) == 4
        assert set(await _outbox_statuses(session_factory)) == {
            OutboxMessageStatus.SENT.value
        }

        # ── 2. Simulate a worker crash *after* the handler side effect
        #       but *before* the pending→sent commit: the outbox still
        #       holds the messages as pending (the classic at-least-once
        #       redelivery window) ────────────────────────────────────
        async with session_factory() as session:
            await session.execute(
                update(OutboxMessage).values(
                    status=OutboxMessageStatus.PENDING, published_at=None
                )
            )
            await session.commit()

        # ── 3. The worker re-processes the same batch ────────────────
        tick2 = await process_outbox_job(e2e_container)
        assert tick2["processed"] == 4

        # ── 4. Observable outcome stays exactly-once ─────────────────
        counts = await _domain_counts(session_factory)
        assert counts["accounts"] == 1
        assert counts["transactions"] == 3
        assert counts["outbox"] == 4  # redelivery created no new entries
        assert counts["sync_runs"] == 1
        assert counts["export_runs"] == 0

        assert await _unique_transaction_ids(session_factory) == set(
            E2E_TXN_IDS
        )

        # The transport itself is at-least-once: the same 4 event_ids were
        # POSTed twice (consumers dedupe on event_id/idempotency_key).
        # What never happens is a *new* domain event: no new outbox rows,
        # no duplicated transactions, no export runs.
        assert len(delivery_capture.requests) == 8
        event_id_counts: dict[str, int] = {}
        for eid in delivery_capture.event_ids():
            event_id_counts[eid] = event_id_counts.get(eid, 0) + 1
        assert set(event_id_counts.values()) == {2}  # each redelivered once

        # All messages are sent again (the ack finally committed).
        assert set(await _outbox_statuses(session_factory)) == {
            OutboxMessageStatus.SENT.value
        }
