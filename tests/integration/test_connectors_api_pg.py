"""PG integration: the connector HTTP API exercised against real PostgreSQL.

The unit suite (``tests/test_connectors_multi_connection.py``) covers the
connector API with fake sessions, and the existing integration suite
exercises repositories / the sync engine against real PG.  This module
closes the gap identified in the multi-connection story (t_7d8bc1f2):
the **HTTP connector API** — real FastAPI routes, real JWT auth, real PG
storage — was never run against PostgreSQL.

Proves, end-to-end through ``httpx.ASGITransport`` against the ephemeral
PG/Redis harness:

* two connections for the **same provider** can be created in one tenant
  and are listed side by side (no uniqueness constraint on
  (tenant, provider)); create/read/update/delete lifecycle works
* **tenant isolation**: another tenant's connections are invisible, and
  every by-id operation (get/update/pause/resume/delete/accounts/sync)
  on a foreign ``connection_id`` returns 404
* **pause/resume** flips only the targeted connection; the sibling keeps
  its state
* **account selection** via ``POST /configs/{id}/accounts`` persists and
  a follow-up manual sync stores only the selected accounts
* the **audit-log** endpoint is admin-only, tenant-scoped and never
  contains credential material
* **no secret leakage**: the plaintext credential value never appears in
  any API response, in audit entries, or in sanitised error messages
  (the inline-test failure path redacts the submitted secret)
"""

# pyright: basic

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import (
    ConnectorConfig,
    ConnectorHealth,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.container import Container
from finance_sync.db.uow import UnitOfWork
from finance_sync.models import Credential, Tenant, User
from finance_sync.models.enums import SyncRunStatus, UserRole
from finance_sync.services.auth import create_access_token, hash_password

pytestmark = pytest.mark.integration

_INT_SECRET = "integration-test-secret-key-32chars!!"
_INT_MASTER_KEY = "0123456789abcdef" * 4  # 32 bytes hex

_CONFIGS_URL = "/api/v1/connectors/configs"
_AUDIT_URL = "/api/v1/connectors/audit-log"


# ── Harness fixtures (same shape as tests/integration/test_connectors_auth_pg.py)


@pytest.fixture
def api_settings(database_url: str, redis_url: str) -> Settings:
    """Settings pointing the app at the ephemeral harness PG/Redis."""
    return Settings(
        database_url=database_url,  # pyright: ignore[reportArgumentType]
        redis_url=redis_url,  # pyright: ignore[reportArgumentType]
        secret_key=_INT_SECRET,  # pyright: ignore[reportArgumentType]
        master_encryption_key=_INT_MASTER_KEY,  # pyright: ignore[reportArgumentType]
    )


@pytest.fixture
def api_container(
    api_settings: Settings,
    pg_engine: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> Container:
    """DI container bound to the harness engine (NullPool, loop-safe)."""
    container = Container.from_settings(api_settings)
    container._engine = pg_engine  # pyright: ignore[reportPrivateUsage]
    container._session_factory = session_factory  # pyright: ignore[reportPrivateUsage]
    return container


@pytest.fixture
def api_app(api_settings: Settings, api_container: Container) -> Any:
    """FastAPI app with the container attached (lifespan not run)."""
    app = create_app(settings=api_settings)
    app.state.container = api_container
    return app


@pytest.fixture
async def api_client(api_app: Any) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client against the in-process app."""
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://integration"
    ) as client:
        yield client


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: UserRole,
    slug: str,
    email: str | None = None,
) -> dict[str, Any]:
    """Persist a tenant + user and return a real signed JWT for them."""
    email = email or f"conn-{slug}@finance-sync.local"
    async with session_factory() as session:
        async with UnitOfWork(session) as uow:
            tenant = await uow.tenants.add(
                Tenant(slug=slug, name=f"Connector {slug} tenant")
            )
            user = User(
                email=email,
                tenant_id=str(tenant.id),
                hashed_password=hash_password("integration-password"),
                display_name=f"Connector {slug}",
                role=role,
                is_active=True,
            )
            uow.session.add(user)
        tenant_id = str(tenant.id)
        user_id = str(user.id)

    token = create_access_token(
        {"sub": user_id, "tenant_id": tenant_id, "role": role},
        Settings(secret_key=_INT_SECRET),  # pyright: ignore[reportArgumentType]
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _create_payload(
    *,
    provider: str = "bunq",
    label: str,
    secret: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A create-config body with one distinctive secret value."""
    return {
        "provider_type": provider,
        "description": label,
        "credentials": {"api_key": secret},
        "options": options or {},
    }


def _secret(name: str) -> str:
    """Distinctive, greppable secret value unique per test."""
    return f"{name}-secret-{uuid.uuid4().hex[:8]}"


# ── CRUD: two connections per provider ────────────────────────────────


class TestMultiConnectionCrudHttpPg:
    """Two simultaneous connections for the same provider, over HTTP+PG."""

    async def test_two_connections_same_provider_created_and_listed(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="crud-a"
        )
        headers = seeded["headers"]
        secret_a = _secret("alpha")
        secret_b = _secret("beta")

        resp_a = await api_client.post(
            _CONFIGS_URL,
            headers=headers,
            json=_create_payload(
                label="Bunq personal", secret=secret_a, provider="bunq"
            ),
        )
        resp_b = await api_client.post(
            _CONFIGS_URL,
            headers=headers,
            json=_create_payload(
                label="Bunq joint",
                secret=secret_b,
                provider="bunq",
                options={"base_url": "https://sandbox.example"},
            ),
        )
        assert resp_a.status_code == 201, resp_a.text
        assert resp_b.status_code == 201, resp_b.text

        body_a = resp_a.json()
        body_b = resp_b.json()
        # Stable connection id in both fields; distinct per connection.
        assert body_a["id"] and body_a["connection_id"] == body_a["id"]
        assert body_b["id"] != body_a["id"]
        assert body_a["provider_type"] == body_b["provider_type"] == "bunq"
        assert body_a["status"] == body_b["status"] == "active"
        assert body_a["is_configured"] is True
        assert body_a["description"] == "Bunq personal"
        assert body_b["description"] == "Bunq joint"

        listing = await api_client.get(_CONFIGS_URL, headers=headers)
        assert listing.status_code == 200
        items = listing.json()
        assert {i["id"] for i in items} == {body_a["id"], body_b["id"]}
        assert {i["description"] for i in items} == {
            "Bunq personal",
            "Bunq joint",
        }

        single = await api_client.get(
            f"{_CONFIGS_URL}/{body_a['id']}", headers=headers
        )
        assert single.status_code == 200
        assert single.json()["id"] == body_a["id"]
        assert single.json()["options"] == {}

    async def test_update_and_delete_are_connection_scoped(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="crud-b"
        )
        headers = seeded["headers"]
        created = [
            (
                await api_client.post(
                    _CONFIGS_URL,
                    headers=headers,
                    json=_create_payload(
                        label="One", secret=_secret("one"), provider="bunq"
                    ),
                )
            ).json(),
            (
                await api_client.post(
                    _CONFIGS_URL,
                    headers=headers,
                    json=_create_payload(
                        label="Two",
                        secret=_secret("two"),
                        provider="trading212",
                    ),
                )
            ).json(),
        ]
        target = created[0]["id"]
        sibling = created[1]["id"]

        updated = await api_client.put(
            f"{_CONFIGS_URL}/{target}",
            headers=headers,
            json={
                "description": "One (renamed)",
                "options": {"base_url": "https://renewed.example"},
            },
        )
        assert updated.status_code == 200, updated.text
        updated_body = updated.json()
        assert updated_body["description"] == "One (renamed)"
        assert updated_body["options"]["base_url"] == "https://renewed.example"

        # The sibling is untouched by the update.
        sibling_row = await api_client.get(
            f"{_CONFIGS_URL}/{sibling}", headers=headers
        )
        assert sibling_row.json()["description"] == "Two"

        deleted = await api_client.delete(
            f"{_CONFIGS_URL}/{target}", headers=headers
        )
        assert deleted.status_code == 204, deleted.text

        listing = await api_client.get(_CONFIGS_URL, headers=headers)
        assert [i["id"] for i in listing.json()] == [sibling]

        # Deleting the same id again → 404.
        again = await api_client.delete(
            f"{_CONFIGS_URL}/{target}", headers=headers
        )
        assert again.status_code == 404


# ── Tenant isolation ──────────────────────────────────────────────────


class TestTenantIsolationHttpPg:
    """Connections of one tenant are invisible and unreachable to another."""

    async def test_list_is_tenant_scoped(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant_a = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="iso-a"
        )
        tenant_b = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="iso-b"
        )
        for seeded, n in ((tenant_a, 2), (tenant_b, 1)):
            for i in range(n):
                resp = await api_client.post(
                    _CONFIGS_URL,
                    headers=seeded["headers"],
                    json=_create_payload(
                        label=f"Conn {i}", secret=_secret(f"iso-{i}")
                    ),
                )
                assert resp.status_code == 201, resp.text

        list_a = await api_client.get(_CONFIGS_URL, headers=tenant_a["headers"])
        list_b = await api_client.get(_CONFIGS_URL, headers=tenant_b["headers"])
        assert len(list_a.json()) == 2
        assert len(list_b.json()) == 1
        assert {c["id"] for c in list_a.json()}.isdisjoint(
            {c["id"] for c in list_b.json()}
        )

    async def test_foreign_connection_operations_all_404(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        owner = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="iso-owner"
        )
        attacker = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="iso-attacker"
        )
        created = (
            await api_client.post(
                _CONFIGS_URL,
                headers=owner["headers"],
                json=_create_payload(
                    label="Mine", secret=_secret("mine"), provider="bunq"
                ),
            )
        ).json()
        foreign_id = created["id"]
        theirs = attacker["headers"]

        assert (
            await api_client.get(f"{_CONFIGS_URL}/{foreign_id}", headers=theirs)
        ).status_code == 404
        assert (
            await api_client.put(
                f"{_CONFIGS_URL}/{foreign_id}",
                headers=theirs,
                json={"description": "stolen"},
            )
        ).status_code == 404
        assert (
            await api_client.post(
                f"{_CONFIGS_URL}/{foreign_id}/pause", headers=theirs
            )
        ).status_code == 404
        assert (
            await api_client.post(
                f"{_CONFIGS_URL}/{foreign_id}/resume", headers=theirs
            )
        ).status_code == 404
        assert (
            await api_client.post(
                f"{_CONFIGS_URL}/{foreign_id}/accounts",
                headers=theirs,
                json={"account_ids": ["x"]},
            )
        ).status_code == 404
        assert (
            await api_client.post(
                f"{_CONFIGS_URL}/{foreign_id}/test", headers=theirs
            )
        ).status_code == 404
        assert (
            await api_client.delete(
                f"{_CONFIGS_URL}/{foreign_id}", headers=theirs
            )
        ).status_code == 404
        assert (
            await api_client.post(
                f"/api/v1/sync/connections/{foreign_id}", headers=theirs
            )
        ).status_code == 404

        # The owner's row is unharmed.
        still_there = await api_client.get(
            f"{_CONFIGS_URL}/{foreign_id}", headers=owner["headers"]
        )
        assert still_there.status_code == 200


# ── Pause / resume ────────────────────────────────────────────────────


class TestPauseResumeHttpPg:
    """Pausing one connection never affects the sibling."""

    async def test_pause_resume_independent_per_connection(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="pause"
        )
        headers = seeded["headers"]
        ids = []
        for label in ("A", "B"):
            resp = await api_client.post(
                _CONFIGS_URL,
                headers=headers,
                json=_create_payload(
                    label=label, secret=_secret(f"pause-{label}")
                ),
            )
            ids.append(resp.json()["id"])

        paused = await api_client.post(
            f"{_CONFIGS_URL}/{ids[0]}/pause", headers=headers
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"

        # Sibling stays active.
        sibling = await api_client.get(
            f"{_CONFIGS_URL}/{ids[1]}", headers=headers
        )
        assert sibling.json()["status"] == "active"

        # Pausing again is idempotent.
        again = await api_client.post(
            f"{_CONFIGS_URL}/{ids[0]}/pause", headers=headers
        )
        assert again.json()["status"] == "paused"

        resumed = await api_client.post(
            f"{_CONFIGS_URL}/{ids[0]}/resume", headers=headers
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "active"
        sibling_after = await api_client.get(
            f"{_CONFIGS_URL}/{ids[1]}", headers=headers
        )
        assert sibling_after.json()["status"] == "active"


# ── Account selection + sync filtering ────────────────────────────────


class _StaticConnector(Connector):
    """A Connector that returns fixed accounts/transactions (per test)."""

    display_name = "Static (multi-conn API)"
    accounts: list[RawAccount] = []
    transactions: list[RawTransaction] = []
    health_error: str | None = None

    @property
    def name(self) -> str:
        return "mock_api"

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        self.supported_resources: frozenset[str] = frozenset()

    async def authenticate(self) -> None:
        return None

    async def health(self) -> ConnectorHealth:
        if self.health_error:
            raise RuntimeError(self.health_error)
        return ConnectorHealth(healthy=True, provider_type=self.name)

    async def fetch_accounts(self) -> list[RawAccount]:
        return list(self.accounts)

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        txns = self.transactions
        if account_id is not None:
            txns = [t for t in txns if t.external_account_id == account_id]
        return list(txns)

    async def _rate_limited_fetch_accounts(self) -> list[RawAccount]:
        return await self.fetch_accounts()

    async def _rate_limited_fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        return await self.fetch_transactions(
            since, account_id=account_id, limit=limit
        )


def _raw_account(ext_id: str, name: str) -> RawAccount:
    return RawAccount(
        external_account_id=ext_id,
        name=name,
        account_type="checking",
        currency_code="EUR",
        current_balance=Decimal("100.00"),
    )


def _raw_txn(ext_id: str, account_id: str, amount: str) -> RawTransaction:
    return RawTransaction(
        external_transaction_id=ext_id,
        external_account_id=account_id,
        amount=Decimal(amount),
        currency_code="EUR",
        occurred_at=datetime.now(UTC) - timedelta(days=1),
        description=f"txn {ext_id}",
        transaction_type="payment",
        status="booked",
    )


class TestAccountSelectionHttpPg:
    """Selection set via the API drives which accounts get synced."""

    async def test_selected_accounts_persist_and_sync_filters_to_them(
        self,
        api_client: httpx.AsyncClient,
        api_app: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import patch

        from finance_sync.models import Account, SyncRun

        seeded = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="sel"
        )
        headers = seeded["headers"]

        registry = ConnectorRegistry()
        _StaticConnector.accounts = [
            _raw_account("keep-me", "Keep Me"),
            _raw_account("skip-me", "Skip Me"),
        ]
        _StaticConnector.transactions = [
            _raw_txn("tx-1", "keep-me", "-10.00"),
            _raw_txn("tx-2", "skip-me", "-20.00"),
        ]
        registry.register_class("mock_api", _StaticConnector, replace=True)

        # The create endpoint validates the provider against the configs
        # module registry; the sync endpoint uses the sync module's.
        with patch(
            "finance_sync.api.v1.connectors_config._get_registry",
            return_value=registry,
        ):
            created = (
                await api_client.post(
                    _CONFIGS_URL,
                    headers=headers,
                    json=_create_payload(
                        label="Mock",
                        secret=_secret("mock"),
                        provider="mock_api",
                    ),
                )
            ).json()
        conn_id = created["id"]

        selected = await api_client.post(
            f"{_CONFIGS_URL}/{conn_id}/accounts",
            headers=headers,
            json={"account_ids": ["keep-me"]},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["selected_accounts"] == ["keep-me"]

        # Reset to "sync all" with an empty list.
        reset = await api_client.post(
            f"{_CONFIGS_URL}/{conn_id}/accounts",
            headers=headers,
            json={"account_ids": []},
        )
        assert reset.json()["selected_accounts"] is None

        # Re-select and sync through the real API: only the selected
        # account may be stored.
        await api_client.post(
            f"{_CONFIGS_URL}/{conn_id}/accounts",
            headers=headers,
            json={"account_ids": ["keep-me"]},
        )
        with patch(
            "finance_sync.api.v1.sync.ConnectorRegistry", return_value=registry
        ):
            sync_resp = await api_client.post(
                f"/api/v1/sync/connections/{conn_id}", headers=headers
            )
        assert sync_resp.status_code == 202, sync_resp.text
        assert sync_resp.json()["accounts_synced"] == 1

        async with session_factory() as s:
            accounts = (
                await s.scalars(
                    select(Account).where(Account.connection_id == conn_id)
                )
            ).all()
            assert [a.external_account_id for a in accounts] == ["keep-me"]
            runs = (
                await s.scalars(
                    select(SyncRun).where(SyncRun.connection_id == conn_id)
                )
            ).all()
            assert len(runs) == 1
            assert runs[0].status == SyncRunStatus.COMPLETED

        # The selection change never deleted imported history without an
        # explicit purge flag.
        narrowed = await api_client.post(
            f"{_CONFIGS_URL}/{conn_id}/accounts",
            headers=headers,
            json={"account_ids": []},
        )
        assert narrowed.status_code == 200
        async with session_factory() as s:
            accounts = (
                await s.scalars(
                    select(Account).where(Account.connection_id == conn_id)
                )
            ).all()
            assert len(accounts) == 1, "history kept without purge_unselected"


# ── Audit log ─────────────────────────────────────────────────────────


class TestAuditLogHttpPg:
    """The audit endpoint is admin-only and tenant-scoped over HTTP+PG."""

    async def test_audit_log_records_lifecycle_and_is_tenant_scoped(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant_a = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="audit-a"
        )
        tenant_b = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="audit-b"
        )
        headers_a = tenant_a["headers"]

        created = (
            await api_client.post(
                _CONFIGS_URL,
                headers=headers_a,
                json=_create_payload(
                    label="Audited", secret=_secret("audited"), provider="bunq"
                ),
            )
        ).json()
        conn_id = created["id"]
        await api_client.post(
            f"{_CONFIGS_URL}/{conn_id}/pause", headers=headers_a
        )
        await api_client.post(
            f"{_CONFIGS_URL}/{conn_id}/resume", headers=headers_a
        )
        await api_client.post(
            f"{_CONFIGS_URL}/{conn_id}/accounts",
            headers=headers_a,
            json={"account_ids": ["acc-1"]},
        )
        # A write from the other tenant must not produce entries here.
        await api_client.post(
            _CONFIGS_URL,
            headers=tenant_b["headers"],
            json=_create_payload(
                label="Other", secret=_secret("other"), provider="trading212"
            ),
        )

        audit_a = await api_client.get(_AUDIT_URL, headers=headers_a)
        assert audit_a.status_code == 200
        entries_a = audit_a.json()
        actions = [e["action"] for e in entries_a]
        assert {"create", "pause", "resume", "select_accounts"} <= set(actions)
        assert all(e["connection_id"] == conn_id for e in entries_a)
        assert all(e["provider_key"] == "bunq" for e in entries_a), (
            "the other tenant's trading212 entry must not leak in"
        )

        audit_b = await api_client.get(_AUDIT_URL, headers=tenant_b["headers"])
        assert len(audit_b.json()) == 1
        assert audit_b.json()[0]["provider_key"] == "trading212"

    async def test_audit_log_rejects_non_admin(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _seed_user(
            session_factory, role=UserRole.USER, slug="audit-user"
        )
        resp = await api_client.get(_AUDIT_URL, headers=user["headers"])
        assert resp.status_code == 403


# ── No secret leakage ─────────────────────────────────────────────────


class TestNoSecretLeakageHttpPg:
    """Plaintext credentials never leave the server, even on failure."""

    async def test_credentials_absent_from_all_responses(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        seeded = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="leak"
        )
        headers = seeded["headers"]
        secret = _secret("leak")
        created_resp = await api_client.post(
            _CONFIGS_URL,
            headers=headers,
            json=_create_payload(
                label="Leak check", secret=secret, provider="bunq"
            ),
        )
        assert created_resp.status_code == 201, created_resp.text
        created = created_resp.json()
        conn_id = created["id"]

        responses: list[httpx.Response] = [
            created_resp,
            await api_client.get(_CONFIGS_URL, headers=headers),
            await api_client.get(f"{_CONFIGS_URL}/{conn_id}", headers=headers),
            await api_client.put(
                f"{_CONFIGS_URL}/{conn_id}",
                headers=headers,
                json={"description": "Renamed"},
            ),
            await api_client.post(
                f"{_CONFIGS_URL}/{conn_id}/pause", headers=headers
            ),
            await api_client.post(
                f"{_CONFIGS_URL}/{conn_id}/resume", headers=headers
            ),
            await api_client.post(
                f"{_CONFIGS_URL}/{conn_id}/accounts",
                headers=headers,
                json={"account_ids": ["acc-1"]},
            ),
            await api_client.get(_AUDIT_URL, headers=headers),
        ]
        for resp in responses:
            assert secret not in resp.text, (
                f"credential leaked in response to {resp.request.url}"
            )

        # And the DB audit trail is clean too.
        async with session_factory() as s:
            from finance_sync.models import ConnectionAuditLog

            rows = (
                await s.scalars(
                    select(ConnectionAuditLog).where(
                        ConnectionAuditLog.tenant_id == seeded["tenant_id"]
                    )
                )
            ).all()
        assert rows, "audit rows expected"
        for row in rows:
            assert secret not in str(row.detail)

    async def test_failed_connection_test_redacts_secret(
        self,
        api_client: httpx.AsyncClient,
        api_app: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import patch

        seeded = await _seed_user(
            session_factory, role=UserRole.ADMIN, slug="redact"
        )
        headers = seeded["headers"]
        secret = _secret("redact")

        registry = ConnectorRegistry()
        _StaticConnector.health_error = (
            f"provider rejected credential '{secret}' (HTTP 401)"
        )
        registry.register_class("mock_api", _StaticConnector, replace=True)

        with patch(
            "finance_sync.api.v1.connectors_config._get_registry",
            return_value=registry,
        ):
            created = (
                await api_client.post(
                    _CONFIGS_URL,
                    headers=headers,
                    json=_create_payload(
                        label="Redact", secret=secret, provider="mock_api"
                    ),
                )
            ).json()
            conn_id = created["id"]

            test_resp = await api_client.post(
                f"{_CONFIGS_URL}/{conn_id}/test", headers=headers
            )
        assert test_resp.status_code == 200
        body = test_resp.json()
        assert body["success"] is False
        assert secret not in body["message"]

        # The stored last_error is sanitised too.
        after = await api_client.get(
            f"{_CONFIGS_URL}/{conn_id}", headers=headers
        )
        assert secret not in (after.json()["last_error"] or "")
        assert after.json()["last_error"]

        audit = await api_client.get(_AUDIT_URL, headers=headers)
        assert secret not in audit.text

        async with session_factory() as s:
            cred = await s.get(Credential, conn_id)
            assert cred is not None
            assert cred.last_error is not None
            assert secret not in cred.last_error
