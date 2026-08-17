"""Tests for the top-level read endpoints (gap G-05 / ms.4.f.1).

Covers:
- OpenAPI registration of GET /transactions, GET /holdings,
  GET /dividends, GET /prices, POST /sync (+ POST /sync/{provider})
- Authentication guards (401 unauthenticated / bad token)
- The ``meta: {asOf, currency, nextCursor, freshness}`` envelope shape
- ReadService unit tests (mocked session) for the new query methods
- Sync trigger behaviour (202 links, per-provider skip, 400 no config)

# pyright: basic
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient

from finance_sync.api.deps.auth import AuthContext, get_auth_context
from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import ReadService

# ── Test helpers ──────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-key-at-least-16-chars"
_MASTER_KEY = "ab" * 32  # 64 hex chars → 32-byte AES-256 key


def _assert_sql_contains(mock: AsyncMock, fragment: str) -> None:
    """Assert that a compiled SQL statement contains ``fragment``."""
    call_args = mock.execute.call_args
    assert call_args is not None
    stmt = str(
        call_args[0][0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert fragment in stmt


def _admin_auth() -> AuthContext:
    """Authenticated principal with the admin role (all permissions)."""
    user = MagicMock()
    user.id = "user-1"
    user.tenant_id = "tenant-1"
    user.role = "admin"
    return AuthContext(user=user)


def _mock_db_session() -> AsyncMock:
    """DB session mock with sane defaults for route-level tests.

    ``execute`` returns a plain MagicMock so chained calls (``.one()``,
    ``.scalars().all()``) resolve synchronously instead of producing
    unawaited coroutines.
    """
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar.return_value = 0
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []
    meta_row = SimpleNamespace(total=0, as_of=None)
    mock_result.one.return_value = meta_row
    session.execute.return_value = mock_result
    return session


# ── Shared fixtures ───────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        master_encryption_key=_MASTER_KEY,
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings=settings)
    app.dependency_overrides[get_db] = lambda: _mock_db_session()
    return app


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed_app(app: FastAPI) -> FastAPI:
    """App where every permission-guarded route passes as admin."""
    app.dependency_overrides[get_auth_context] = _admin_auth
    return app


@pytest.fixture
def authed_client(
    authed_app: FastAPI,
) -> Generator[TestClient, None, None]:
    with TestClient(authed_app) as c:
        yield c


@pytest.fixture
def mock_session() -> AsyncMock:
    """Session mock: empty results + a sane count/max meta row."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar.return_value = 0
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []
    meta_row = SimpleNamespace(total=0, as_of=None)
    mock_result.one.return_value = meta_row
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def svc(mock_session: AsyncMock) -> ReadService:
    return ReadService(mock_session)


# ═══════════════════════════════════════════════════════════════════════
# OpenAPI registration
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAPIRegistration:
    """All five documented top-level endpoints appear in /openapi.json."""

    def test_top_level_endpoints_registered(self, client: TestClient) -> None:
        paths: dict[str, Any] = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/transactions" in paths
        assert paths["/api/v1/transactions"]["get"]["tags"] == ["transactions"]
        assert "/api/v1/holdings" in paths
        assert paths["/api/v1/holdings"]["get"]["tags"] == ["holdings"]
        assert "/api/v1/dividends" in paths
        assert paths["/api/v1/dividends"]["get"]["tags"] == ["dividends"]
        assert "/api/v1/prices" in paths
        assert paths["/api/v1/prices"]["get"]["tags"] == ["prices"]
        assert "/api/v1/sync" in paths
        assert "post" in paths["/api/v1/sync"]
        assert "/api/v1/sync/{provider}" in paths
        assert "post" in paths["/api/v1/sync/{provider}"]

    def test_transactions_filters_in_openapi(self, client: TestClient) -> None:
        params = {
            p["name"]
            for p in client.get("/openapi.json")
            .json()["paths"]["/api/v1/transactions"]["get"]
            .get("parameters", [])
        }
        for expected in (
            "accountId",
            "provider",
            "status",
            "type",
            "currency",
            "from",
            "to",
        ):
            assert expected in params

    def test_prices_filters_in_openapi(self, client: TestClient) -> None:
        params = {
            p["name"]
            for p in client.get("/openapi.json")
            .json()["paths"]["/api/v1/prices"]["get"]
            .get("parameters", [])
        }
        for expected in (
            "securityId",
            "listingId",
            "interval",
            "from",
            "to",
        ):
            assert expected in params


# ═══════════════════════════════════════════════════════════════════════
# Authentication guards
# ═══════════════════════════════════════════════════════════════════════


class TestAuthGuards:
    """Every new endpoint requires authentication."""

    ENDPOINTS = [
        ("GET", "/api/v1/transactions"),
        ("GET", "/api/v1/holdings"),
        ("GET", "/api/v1/dividends"),
        ("GET", "/api/v1/prices"),
        ("POST", "/api/v1/sync"),
        ("POST", "/api/v1/sync/bunq"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_unauthenticated_returns_401(
        self, client: TestClient, method: str, path: str
    ) -> None:
        response: Response = client.request(method, path)
        assert response.status_code == 401
        assert "detail" in response.json()

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_bad_token_returns_401(
        self, client: TestClient, method: str, path: str
    ) -> None:
        headers = {"Authorization": "Bearer invalid-token-here"}
        response: Response = client.request(method, path, headers=headers)
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# Meta envelope shape
# ═══════════════════════════════════════════════════════════════════════


class TestMetaEnvelope:
    """Collection responses carry meta: {asOf, currency, nextCursor,
    freshness}."""

    GET_ENDPOINTS = [
        "/api/v1/transactions",
        "/api/v1/holdings",
        "/api/v1/dividends",
        "/api/v1/prices",
    ]

    @pytest.mark.parametrize("path", GET_ENDPOINTS)
    def test_get_response_has_meta_envelope(
        self, authed_client: TestClient, path: str
    ) -> None:
        response: Response = authed_client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        meta = body["meta"]
        assert set(meta) == {"as_of", "currency", "next_cursor", "freshness"}
        assert meta["freshness"] in {"fresh", "stale", "partial", "unknown"}

    def test_sync_response_has_meta_envelope(self, authed_app: FastAPI) -> None:
        session = AsyncMock()
        cred_result = MagicMock()
        cred_result.scalars.return_value.all.return_value = []
        session.execute.return_value = cred_result
        authed_app.dependency_overrides[get_db] = lambda: session

        fake_result = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            accounts_synced=0,
            transactions_synced=0,
            error_message=None,
            duration_s=0.0,
        )
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=fake_result)

        with (
            patch(
                "finance_sync.api.v1.sync.SyncOrchestrator",
                return_value=fake_orch,
            ),
            patch("finance_sync.api.v1.sync.ConnectorRegistry"),
            TestClient(authed_app) as c,
        ):
            response = c.post("/api/v1/sync", json={"providers": ["bunq"]})
        assert response.status_code == 202
        body = response.json()
        assert "sync_runs" in body
        meta = body["meta"]
        assert set(meta) == {"as_of", "currency", "next_cursor", "freshness"}


# ═══════════════════════════════════════════════════════════════════════
# ReadService unit tests (mocked session)
# ═══════════════════════════════════════════════════════════════════════


class TestReadServiceTopLevelTransactions:
    """ReadService.list_transactions() behaviour."""

    async def test_empty_tenant(self, svc: ReadService) -> None:
        result = await svc.list_transactions(tenant_id="t1")
        assert result.total == 0
        assert result.items == []
        assert result.meta.freshness == "unknown"

    async def test_passes_tenant_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_transactions(tenant_id="tenant-abc")
        _assert_sql_contains(mock_session, "tenant-abc")

    async def test_account_and_provider_filters(
        self, mock_session: AsyncMock
    ) -> None:
        svc = ReadService(mock_session)
        await svc.list_transactions(
            tenant_id="t1", account_id="acct-1", provider_key="bunq"
        )
        _assert_sql_contains(mock_session, "acct-1")
        _assert_sql_contains(mock_session, "bunq")

    async def test_status_type_currency_filters(
        self, mock_session: AsyncMock
    ) -> None:
        svc = ReadService(mock_session)
        await svc.list_transactions(
            tenant_id="t1",
            status="booked",
            transaction_type="payment",
            currency_code="EUR",
        )
        _assert_sql_contains(mock_session, "booked")
        _assert_sql_contains(mock_session, "payment")
        _assert_sql_contains(mock_session, "EUR")

    async def test_date_range_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        since = datetime(2025, 1, 1, tzinfo=UTC)
        until = datetime(2025, 6, 30, tzinfo=UTC)
        await svc.list_transactions(
            tenant_id="t1", date_from=since, date_to=until
        )
        _assert_sql_contains(mock_session, "2025-01-01")
        _assert_sql_contains(mock_session, "2025-06-30")

    async def test_meta_as_of_tracks_latest_observation(
        self, mock_session: AsyncMock
    ) -> None:
        latest = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)
        mock_session.execute.return_value.one.return_value = SimpleNamespace(
            total=4, as_of=latest
        )
        svc = ReadService(mock_session)
        result = await svc.list_transactions(tenant_id="t1")
        assert result.total == 4
        assert result.meta.as_of == latest


class TestReadServiceTopLevelHoldings:
    """ReadService.get_holdings() behaviour."""

    async def test_empty_tenant(self, svc: ReadService) -> None:
        result = await svc.get_holdings(tenant_id="t1")
        assert result.total == 0
        assert result.items == []
        assert result.meta.as_of is None

    async def test_passes_tenant_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.get_holdings(tenant_id="tenant-xyz")
        _assert_sql_contains(mock_session, "tenant-xyz")

    async def test_security_and_account_filters(
        self, mock_session: AsyncMock
    ) -> None:
        svc = ReadService(mock_session)
        await svc.get_holdings(
            tenant_id="t1", account_id="acct-7", security_id="sec-9"
        )
        _assert_sql_contains(mock_session, "acct-7")
        _assert_sql_contains(mock_session, "sec-9")

    async def test_as_of_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        as_of = datetime(2025, 4, 1, tzinfo=UTC)
        await svc.get_holdings(tenant_id="t1", as_of=as_of)
        _assert_sql_contains(mock_session, "2025-04-01")


class TestReadServiceTopLevelDividends:
    """ReadService.list_dividends() behaviour."""

    async def test_empty_tenant(self, svc: ReadService) -> None:
        result = await svc.list_dividends(tenant_id="t1")
        assert result.total == 0
        assert result.items == []

    async def test_filters_dividend_type(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_dividends(tenant_id="t1")
        _assert_sql_contains(mock_session, "dividend")

    async def test_account_security_filters(
        self, mock_session: AsyncMock
    ) -> None:
        svc = ReadService(mock_session)
        await svc.list_dividends(
            tenant_id="t1", account_id="acct-2", security_id="sec-3"
        )
        _assert_sql_contains(mock_session, "acct-2")
        _assert_sql_contains(mock_session, "sec-3")


class TestReadServiceTopLevelPrices:
    """ReadService.get_prices() behaviour."""

    async def test_series_mode_filters_security(
        self, mock_session: AsyncMock
    ) -> None:
        svc = ReadService(mock_session)
        await svc.get_prices(security_id="sec-1")
        _assert_sql_contains(mock_session, "sec-1")

    async def test_latest_mode_uses_interval(
        self, mock_session: AsyncMock
    ) -> None:
        svc = ReadService(mock_session)
        await svc.get_prices(interval="1d")
        _assert_sql_contains(mock_session, "1d")

    async def test_resolve_listing_security_id(
        self, mock_session: AsyncMock
    ) -> None:
        mock_session.execute.return_value.scalar_one_or_none.return_value = (
            "sec-42"
        )
        svc = ReadService(mock_session)
        result = await svc.resolve_listing_security_id("listing-1")
        assert result == "sec-42"
        _assert_sql_contains(mock_session, "listing-1")


# ═══════════════════════════════════════════════════════════════════════
# Sync trigger behaviour
# ═══════════════════════════════════════════════════════════════════════


class TestSyncTrigger:
    """POST /sync + POST /sync/{provider} behaviour."""

    def _session_with_credentials(self, providers: list[str]) -> AsyncMock:
        session = AsyncMock()
        cred_result = MagicMock()
        cred_result.scalars.return_value.all.return_value = [
            SimpleNamespace(
                id=f"conn-{index}",
                provider_key=p,
                status="active",
                selected_accounts=None,
                encrypted_payload=b"\x00" * 32,
                nonce=b"\x00" * 12,
            )
            for index, p in enumerate(providers)
        ]
        # SyncRun id lookup returns None (no run rows).
        cred_result.scalar_one_or_none.return_value = None
        session.execute.return_value = cred_result
        return session

    def _decrypt_patch(self):
        """Stub credential decryption (real AES would raise InvalidTag)."""
        return patch(
            "finance_sync.api.v1.sync.decrypt_credential",
            return_value='{"api_key": "test-secret"}',
        )

    def _container_patch(self, settings: Settings):
        """Fake DI container (session_factory raises without a DB)."""
        return patch(
            "finance_sync.api.v1.sync.get_container",
            return_value=SimpleNamespace(
                settings=settings,
                session_factory=MagicMock(),
            ),
        )

    def test_no_providers_returns_400(self, authed_app: FastAPI) -> None:
        session = self._session_with_credentials([])
        authed_app.dependency_overrides[get_db] = lambda: session
        with TestClient(authed_app) as c:
            response = c.post("/api/v1/sync", json={})
        assert response.status_code == 400

    def test_skips_provider_without_credentials(
        self, authed_app: FastAPI
    ) -> None:
        session = self._session_with_credentials(["trading212"])
        authed_app.dependency_overrides[get_db] = lambda: session
        with TestClient(authed_app) as c:
            response = c.post("/api/v1/sync", json={"providers": ["bunq"]})
        assert response.status_code == 202
        body = response.json()
        assert body["sync_runs"][0]["provider"] == "bunq"
        assert body["sync_runs"][0]["status"] == "skipped"

    def test_runs_sync_for_configured_provider(
        self, authed_app: FastAPI, settings: Settings
    ) -> None:
        session = self._session_with_credentials(["bunq"])
        authed_app.dependency_overrides[get_db] = lambda: session

        fake_result = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            accounts_synced=2,
            transactions_synced=9,
            holdings_synced=0,
            unresolved_securities=0,
            error_message=None,
            duration_s=1.25,
        )
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=fake_result)

        with (
            patch(
                "finance_sync.api.v1.sync.SyncOrchestrator",
                return_value=fake_orch,
            ),
            patch("finance_sync.api.v1.sync.ConnectorRegistry"),
            self._decrypt_patch(),
            self._container_patch(settings),
            TestClient(authed_app) as c,
        ):
            response = c.post("/api/v1/sync", json={"providers": ["bunq"]})
        assert response.status_code == 202
        run = response.json()["sync_runs"][0]
        assert run["provider"] == "bunq"
        assert run["status"] == "completed"
        assert run["accounts_synced"] == 2
        assert run["transactions_synced"] == 9
        # The run is scoped to the connection it belongs to.
        assert run["connection_id"] == "conn-0"
        fake_orch.run_sync.assert_awaited_once()
        call_kwargs = fake_orch.run_sync.await_args.kwargs
        assert call_kwargs["connection_id"] == "conn-0"

    def test_two_connections_same_provider_sync_independently(
        self, authed_app: FastAPI, settings: Settings
    ) -> None:
        """Two bunq connections produce two independent sync runs."""
        session = self._session_with_credentials(["bunq", "bunq"])
        authed_app.dependency_overrides[get_db] = lambda: session

        fake_result = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            accounts_synced=1,
            transactions_synced=2,
            holdings_synced=0,
            unresolved_securities=0,
            error_message=None,
            duration_s=0.5,
        )
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=fake_result)

        with (
            patch(
                "finance_sync.api.v1.sync.SyncOrchestrator",
                return_value=fake_orch,
            ),
            patch("finance_sync.api.v1.sync.ConnectorRegistry"),
            self._decrypt_patch(),
            self._container_patch(settings),
            TestClient(authed_app) as c,
        ):
            response = c.post("/api/v1/sync", json={"providers": ["bunq"]})
        assert response.status_code == 202
        runs = response.json()["sync_runs"]
        assert len(runs) == 2
        assert {r["connection_id"] for r in runs} == {"conn-0", "conn-1"}
        assert fake_orch.run_sync.await_count == 2

    def test_paused_connection_skipped_by_provider_trigger(
        self, authed_app: FastAPI, settings: Settings
    ) -> None:
        session = AsyncMock()
        cred_result = MagicMock()
        cred_result.scalars.return_value.all.return_value = [
            SimpleNamespace(
                id="conn-active",
                provider_key="bunq",
                status="active",
                selected_accounts=None,
                encrypted_payload=b"\x00" * 32,
                nonce=b"\x00" * 12,
            ),
            SimpleNamespace(
                id="conn-paused",
                provider_key="bunq",
                status="paused",
                selected_accounts=None,
                encrypted_payload=b"\x00" * 32,
                nonce=b"\x00" * 12,
            ),
        ]
        cred_result.scalar_one_or_none.return_value = None
        session.execute.return_value = cred_result
        authed_app.dependency_overrides[get_db] = lambda: session

        fake_result = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            accounts_synced=1,
            transactions_synced=1,
            holdings_synced=0,
            unresolved_securities=0,
            error_message=None,
            duration_s=0.5,
        )
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=fake_result)

        with (
            patch(
                "finance_sync.api.v1.sync.SyncOrchestrator",
                return_value=fake_orch,
            ),
            patch("finance_sync.api.v1.sync.ConnectorRegistry"),
            self._decrypt_patch(),
            self._container_patch(settings),
            TestClient(authed_app) as c,
        ):
            response = c.post("/api/v1/sync", json={"providers": ["bunq"]})
        assert response.status_code == 202
        runs = response.json()["sync_runs"]
        by_conn = {r["connection_id"]: r for r in runs}
        assert by_conn["conn-active"]["status"] == "completed"
        assert by_conn["conn-paused"]["status"] == "skipped"
        # Only the active connection actually ran.
        assert fake_orch.run_sync.await_count == 1

    def test_manual_connection_sync_endpoint(
        self, authed_app: FastAPI, settings: Settings
    ) -> None:
        """POST /sync/connections/{id} syncs exactly that connection."""
        cred = SimpleNamespace(
            id="conn-42",
            provider_key="trading212",
            status="paused",  # manual sync runs even when paused
            selected_accounts=["acc_1"],
            encrypted_payload=b"\x00" * 32,
            nonce=b"\x00" * 12,
        )
        session = AsyncMock()
        cred_result = MagicMock()
        cred_result.scalar_one_or_none.return_value = cred
        session.execute.return_value = cred_result
        authed_app.dependency_overrides[get_db] = lambda: session

        fake_result = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            accounts_synced=1,
            transactions_synced=4,
            holdings_synced=3,
            unresolved_securities=0,
            error_message=None,
            duration_s=2.0,
        )
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=fake_result)

        with (
            patch(
                "finance_sync.api.v1.sync.SyncOrchestrator",
                return_value=fake_orch,
            ),
            patch("finance_sync.api.v1.sync.ConnectorRegistry"),
            self._decrypt_patch(),
            self._container_patch(settings),
            TestClient(authed_app) as c,
        ):
            response = c.post("/api/v1/sync/connections/conn-42")
        assert response.status_code == 202
        body = response.json()
        assert body["connection_id"] == "conn-42"
        assert body["status"] == "completed"
        call_kwargs = fake_orch.run_sync.await_args.kwargs
        assert call_kwargs["connection_id"] == "conn-42"
        assert call_kwargs["selected_accounts"] == ["acc_1"]

    def test_manual_connection_sync_404_for_foreign_connection(
        self, authed_app: FastAPI
    ) -> None:
        session = AsyncMock()
        cred_result = MagicMock()
        cred_result.scalar_one_or_none.return_value = None
        session.execute.return_value = cred_result
        authed_app.dependency_overrides[get_db] = lambda: session
        with TestClient(authed_app) as c:
            response = c.post("/api/v1/sync/connections/nope")
        assert response.status_code == 404

    def test_provider_path_endpoint(
        self, authed_app: FastAPI, settings: Settings
    ) -> None:
        session = self._session_with_credentials(["bunq"])
        authed_app.dependency_overrides[get_db] = lambda: session

        fake_result = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            accounts_synced=1,
            transactions_synced=3,
            holdings_synced=0,
            unresolved_securities=0,
            error_message=None,
            duration_s=0.5,
        )
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=fake_result)

        with (
            patch(
                "finance_sync.api.v1.sync.SyncOrchestrator",
                return_value=fake_orch,
            ),
            patch("finance_sync.api.v1.sync.ConnectorRegistry"),
            self._decrypt_patch(),
            self._container_patch(settings),
            TestClient(authed_app) as c,
        ):
            response = c.post("/api/v1/sync/bunq")
        assert response.status_code == 202
        assert response.json()["sync_runs"][0]["provider"] == "bunq"
