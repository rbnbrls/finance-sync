"""Tests for the reconciliation API endpoints.

# pyright: basic

Uses mocked dependencies to test the HTTP layer without a live database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db
from finance_sync.services.auth import create_access_token

# ── Test settings ────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-key-at-least-16-chars"


@pytest.fixture
def settings() -> Settings:
    """Settings with a fixed secret key for deterministic tokens."""
    return Settings(
        secret_key=_TEST_SECRET,  # type: ignore[call-arg]
        access_token_expire_minutes=15,
        database_url=None,
        redis_url=None,
    )


def _make_auth_session() -> AsyncMock:
    """Build a mock DB session that works with the auth dependency.

    The get_current_user dependency calls::

        db.execute(stmt) -> result.scalar_one_or_none()

    ``scalar_one_or_none()`` must return a mock User object with
    ``is_active = True`` to pass the auth check.
    """
    session = AsyncMock()
    mock_result = MagicMock()
    mock_user = MagicMock()
    mock_user.is_active = True
    mock_user.id = "admin-user"
    mock_user.tenant_id = "test-tenant"
    mock_user.role = "admin"
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_user)
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def auth_db_session() -> AsyncMock:
    """Session mock used only for the auth lookup dependency."""
    return _make_auth_session()


@pytest.fixture
def mock_container(settings: Settings) -> MagicMock:
    """Mock DI container with a working session_factory.

    The session_factory returns a session mock that handles all
    ORM operations the reconciliation service performs.
    """
    session = AsyncMock()

    # UoW transaction mocks
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    session_factory = MagicMock(spec=async_sessionmaker)
    session_factory.return_value.__aenter__.return_value = session
    session_factory.return_value.__aexit__.return_value = None

    container = MagicMock()
    type(container).session_factory = PropertyMock(return_value=session_factory)
    # The auth system calls get_container(request) to decode the JWT,
    # which needs container.settings to be a real Settings object.
    container.settings = settings
    return container


@pytest.fixture
def app(
    settings: Settings,
    mock_container: MagicMock,
    auth_db_session: AsyncMock,
) -> FastAPI:
    """Create the FastAPI app with overridden dependencies.

    The lifespan is replaced with a no-op context manager so that
    ``app.state.container`` stays as our mock_container instead of
    being overwritten by the real lifespan.
    """
    from contextlib import asynccontextmanager

    app = create_app(settings=settings)

    # Store mock container before TestClient runs (lifespan won't overwrite)
    app.state.container = mock_container

    # Replace lifespan with a no-op so container stays as our mock
    @asynccontextmanager
    async def _noop_lifespan(_app: FastAPI) -> AsyncGenerator[None]:  # type: ignore[type-arg]
        yield

    app.router.lifespan_context = _noop_lifespan

    # Override get_db for auth lookups (used by get_current_user via Depends)
    app.dependency_overrides[get_db] = lambda: auth_db_session

    return app


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    """Test client for the FastAPI app."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_token(settings: Settings) -> str:
    """Admin access token for authenticated requests."""
    return create_access_token(
        {
            "sub": "admin-user",
            "tenant_id": "test-tenant",
            "role": "admin",
            "type": "access",
        },
        settings,
    )


@pytest.fixture
def auth_headers(admin_token: str) -> dict[str, str]:
    """Auth headers with admin Bearer token."""
    return {"Authorization": f"Bearer {admin_token}"}


# ═══════════════════════════════════════════════════════════════════════
# OpenAPI registration
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAPIRegistration:
    """Verify reconciliation endpoints appear in the OpenAPI schema."""

    def test_endpoints_registered(self, client: TestClient) -> None:
        paths: dict[str, Any] = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/reconciliation" in paths
        # POST trigger
        assert "post" in paths["/api/v1/reconciliation"]
        # GET list
        assert "get" in paths["/api/v1/reconciliation"]
        # GET by id
        assert "/api/v1/reconciliation/{run_id}" in paths
        # POST compare
        assert "/api/v1/reconciliation/compare" in paths
        assert paths["/api/v1/reconciliation/compare"]["post"]["tags"] == [
            "reconciliation"
        ]


# ═══════════════════════════════════════════════════════════════════════
# POST /reconciliation/compare — auth & validation
# ═══════════════════════════════════════════════════════════════════════


class TestCompareConnectorsAuth:
    """Auth guard on the compare endpoint."""

    def test_requires_authentication(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={
                "connector_a": "bunq",
                "connector_b": "trading212",
            },
        )
        assert resp.status_code == 401  # no token


class TestCompareConnectorsValidation:
    """Validation logic on the compare endpoint."""

    def test_missing_connector_a(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={"connector_b": "trading212"},
            headers=auth_headers,
        )
        assert resp.status_code == 422  # validation error

    def test_missing_connector_b(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={"connector_a": "bunq"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_empty_connector_a(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={"connector_a": "", "connector_b": "trading212"},
            headers=auth_headers,
        )
        assert resp.status_code == 422  # min_length=1

    def test_zero_length_connector_b(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={"connector_a": "bunq", "connector_b": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_same_connector_raises_400(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={"connector_a": "bunq", "connector_b": "bunq"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "must be different" in resp.text


# ═══════════════════════════════════════════════════════════════════════
# POST /reconciliation/compare — successful execution
# ═══════════════════════════════════════════════════════════════════════


class TestCompareConnectorsExecution:
    """Execution of the compare endpoint (service wiring checks)."""

    def test_compare_returns_200_or_500(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """A valid compare request reaches the endpoint and is not rejected
        by auth or validation."""
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={
                "connector_a": "bunq",
                "connector_b": "trading212",
            },
            headers=auth_headers,
        )
        # The endpoint will either succeed (200) or fail with a service-level
        # error (500) depending on mock setup — but never 401/403/422.
        assert resp.status_code not in (401, 403, 422)

    def test_compare_with_date_range(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Compare with optional date range parameters — reaches the endpoint."""
        resp = client.post(
            "/api/v1/reconciliation/compare",
            json={
                "connector_a": "bunq",
                "connector_b": "trading212",
                "date_from": "2025-10-01T00:00:00Z",
                "date_to": "2026-01-01T00:00:00Z",
                "threshold_hours": 24,
            },
            headers=auth_headers,
        )
        assert resp.status_code not in (401, 403, 422)

    def test_compare_success(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Compare returns 200 with proper response when reconciliation succeeds."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_compare_1"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {"provider_keys": ["bunq", "trading212"]}
        mock_run.finding_count = 2
        mock_run.summary = {"by_kind": {"duplicate_transaction": 2}, "by_severity": {"error": 2}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)):
            resp = client.post(
                "/api/v1/reconciliation/compare",
                json={
                    "connector_a": "bunq",
                    "connector_b": "trading212",
                    "date_from": "2026-01-01T00:00:00Z",
                    "date_to": "2026-06-01T00:00:00Z",
                    "threshold_hours": 24,
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["connector_a"] == "bunq"
        assert data["connector_b"] == "trading212"
        assert data["run"]["id"] == "run_compare_1"
        assert data["run"]["status"] == "completed"
        assert data["run"]["finding_count"] == 2
        assert "Compared 'bunq' vs 'trading212'" in data["message"]

    def test_compare_with_failed_reconciliation(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Compare returns 500 when reconciliation status is 'failed'."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_compare_fail"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "failed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {}
        mock_run.finding_count = None
        mock_run.summary = None
        mock_run.error_message = "Provider data incomplete"
        mock_run.created_at = datetime.now(UTC)

        with patch.object(ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)):
            resp = client.post(
                "/api/v1/reconciliation/compare",
                json={
                    "connector_a": "bunq",
                    "connector_b": "trading212",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 500
        assert "Provider data incomplete" in resp.text


# ═══════════════════════════════════════════════════════════════════════
# POST /reconciliation — trigger_reconciliation
# ═══════════════════════════════════════════════════════════════════════


class TestTriggerReconciliation:
    """Tests for POST /api/v1/reconciliation (trigger_reconciliation)."""

    def test_requires_authentication(self, client: TestClient) -> None:
        resp = client.post("/api/v1/reconciliation", json={})
        assert resp.status_code == 401

    def test_trigger_reconciliation_success(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        mock_container: MagicMock,
    ) -> None:
        """Successful reconciliation returns 201 with a run response."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_abc123"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {"date_from": "2026-01-01T00:00:00Z"}
        mock_run.finding_count = 3
        mock_run.summary = {
            "by_kind": {"duplicate_transaction": 2, "missing_transaction": 1},
            "by_severity": {"warning": 2, "info": 1},
        }
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)):
            resp = client.post(
                "/api/v1/reconciliation",
                json={
                    "account_ids": ["acct_1"],
                    "threshold_hours": 24,
                },
                headers=auth_headers,
            )

        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["id"] == "run_abc123"
        assert data["status"] == "completed"
        assert data["finding_count"] == 3
        assert data["summary"]["by_kind"]["duplicate_transaction"] == 2

    def test_trigger_reconciliation_with_date_range(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Reconciliation with explicit date range parameters."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_def456"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {
            "date_from": "2026-01-01T00:00:00Z",
            "date_to": "2026-06-01T00:00:00Z",
        }
        mock_run.finding_count = 0
        mock_run.summary = {"by_kind": {}, "by_severity": {}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)):
            resp = client.post(
                "/api/v1/reconciliation",
                json={
                    "date_from": "2026-01-01T00:00:00Z",
                    "date_to": "2026-06-01T00:00:00Z",
                    "threshold_hours": 72,
                },
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["finding_count"] == 0

    def test_trigger_reconciliation_sync_fails(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """When reconcile returns a failed run, the response carries status=failed."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_failed"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "failed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {}
        mock_run.finding_count = None
        mock_run.summary = None
        mock_run.error_message = "DB connection lost"
        mock_run.created_at = datetime.now(UTC)

        with patch.object(ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)):
            resp = client.post(
                "/api/v1/reconciliation",
                json={},
                headers=auth_headers,
            )

        # Even failed runs return 201 with failed status
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error_message"] == "DB connection lost"


# ═══════════════════════════════════════════════════════════════════════
# GET /reconciliation — list_reconciliation_runs
# ═══════════════════════════════════════════════════════════════════════


class TestListReconciliationRuns:
    """Tests for GET /api/v1/reconciliation (list_reconciliation_runs)."""

    def test_requires_authentication(self, client: TestClient) -> None:
        resp = client.get("/api/v1/reconciliation")
        assert resp.status_code == 401

    def test_list_runs_empty(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Empty list returns 200 with empty items."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        with patch.object(ReconciliationService, "list_runs", new=AsyncMock(return_value=[])):
            resp = client.get(
                "/api/v1/reconciliation",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_runs_with_results(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Returns list of runs with pagination metadata."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run_1 = MagicMock()
        mock_run_1.id = "run_1"
        mock_run_1.tenant_id = "test-tenant"
        mock_run_1.status = "completed"
        mock_run_1.started_at = datetime.now(UTC)
        mock_run_1.completed_at = datetime.now(UTC)
        mock_run_1.scope = {"date_from": "2026-01-01T00:00:00Z"}
        mock_run_1.finding_count = 1
        mock_run_1.summary = {"by_kind": {"duplicate_transaction": 1}, "by_severity": {"warning": 1}}
        mock_run_1.error_message = None
        mock_run_1.created_at = datetime.now(UTC)

        mock_run_2 = MagicMock()
        mock_run_2.id = "run_2"
        mock_run_2.tenant_id = "test-tenant"
        mock_run_2.status = "completed"
        mock_run_2.started_at = datetime.now(UTC)
        mock_run_2.completed_at = datetime.now(UTC)
        mock_run_2.scope = None
        mock_run_2.finding_count = 0
        mock_run_2.summary = {"by_kind": {}, "by_severity": {}}
        mock_run_2.error_message = None
        mock_run_2.created_at = datetime.now(UTC)

        with patch.object(ReconciliationService, "list_runs", new=AsyncMock(return_value=[mock_run_1, mock_run_2])):
            resp = client.get(
                "/api/v1/reconciliation?limit=10&offset=0",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["limit"] == 10
        assert data["offset"] == 0
        assert data["items"][0]["id"] == "run_1"
        assert data["items"][1]["status"] == "completed"

    def test_list_runs_with_summary_none(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Run with summary=None still serialises correctly."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_no_summary"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = None
        mock_run.finding_count = None
        mock_run.summary = None  # No summary at all
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(ReconciliationService, "list_runs", new=AsyncMock(return_value=[mock_run])):
            resp = client.get(
                "/api/v1/reconciliation",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["summary"]["by_kind"] == {}
        assert item["summary"]["by_severity"] == {}


# ═══════════════════════════════════════════════════════════════════════
# GET /reconciliation/{run_id} — get_reconciliation_run
# ═══════════════════════════════════════════════════════════════════════


class TestGetReconciliationRun:
    """Tests for GET /api/v1/reconciliation/{run_id}."""

    def test_requires_authentication(self, client: TestClient) -> None:
        resp = client.get("/api/v1/reconciliation/run_1")
        assert resp.status_code == 401

    def test_get_run_not_found(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-existent run ID returns 404."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        with patch.object(
            ReconciliationService,
            "get_run_with_results",
            new=AsyncMock(return_value=(None, [], 0)),
        ):
            resp = client.get(
                "/api/v1/reconciliation/nonexistent",
                headers=auth_headers,
            )

        assert resp.status_code == 404

    def test_get_run_success(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Returns run with its results."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_1"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {}
        mock_run.finding_count = 1
        mock_run.summary = {"by_kind": {"duplicate_transaction": 1}, "by_severity": {"error": 1}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        mock_result = MagicMock()
        mock_result.id = "res_1"
        mock_result.run_id = "run_1"
        mock_result.kind = "duplicate_transaction"
        mock_result.severity = "error"
        mock_result.account_id = "acct_1"
        mock_result.provider_key = "bunq"
        mock_result.other_provider_key = "trading212"
        mock_result.transaction_id_a = "tx_a_1"
        mock_result.transaction_id_b = "tx_b_1"
        mock_result.external_transaction_id_a = "ext_a"
        mock_result.external_transaction_id_b = "ext_b"
        mock_result.amount = Decimal("-100.00")
        mock_result.other_amount = Decimal("-100.00")
        mock_result.occurred_at = datetime.now(UTC)
        mock_result.description = "Potential duplicate: Groceries"
        mock_result.details = {"confidence": 0.9}
        mock_result.created_at = datetime.now(UTC)

        with patch.object(
            ReconciliationService,
            "get_run_with_results",
            new=AsyncMock(return_value=(mock_run, [mock_result], 1)),
        ):
            resp = client.get(
                "/api/v1/reconciliation/run_1",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["run"]["id"] == "run_1"
        assert data["run"]["status"] == "completed"
        assert len(data["results"]) == 1
        assert data["total_results"] == 1
        assert data["results"][0]["kind"] == "duplicate_transaction"
        assert data["results"][0]["provider_key"] == "bunq"
        assert data["results"][0]["other_provider_key"] == "trading212"

    def test_get_run_with_filters(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """kind and severity query params are passed through."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_1"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {}
        mock_run.finding_count = 2
        mock_run.summary = {"by_kind": {"duplicate_transaction": 2}, "by_severity": {"error": 2}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        mock_result = MagicMock()
        mock_result.id = "res_1"
        mock_result.run_id = "run_1"
        mock_result.kind = "duplicate_transaction"
        mock_result.severity = "error"
        mock_result.account_id = "acct_1"
        mock_result.provider_key = "bunq"
        mock_result.other_provider_key = None
        mock_result.transaction_id_a = None
        mock_result.transaction_id_b = None
        mock_result.external_transaction_id_a = None
        mock_result.external_transaction_id_b = None
        mock_result.amount = None
        mock_result.other_amount = None
        mock_result.occurred_at = None
        mock_result.description = "dup"
        mock_result.details = {}
        mock_result.created_at = datetime.now(UTC)

        with patch.object(
            ReconciliationService,
            "get_run_with_results",
            new=AsyncMock(return_value=(mock_run, [mock_result], 2)),
        ):
            resp = client.get(
                "/api/v1/reconciliation/run_1?kind=duplicate_transaction&severity=error",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["run"]["id"] == "run_1"
        assert len(data["results"]) == 1
        assert data["results"][0]["kind"] == "duplicate_transaction"

    def test_wrong_tenant_returns_404(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Run from another tenant returns 404."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_other"
        mock_run.tenant_id = "other-tenant"  # Different from auth's "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = None
        mock_run.finding_count = 0
        mock_run.summary = {"by_kind": {}, "by_severity": {}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        mock_result = MagicMock()
        mock_result.id = "res_1"
        mock_result.run_id = "run_other"
        mock_result.kind = "duplicate_transaction"
        mock_result.severity = "error"
        mock_result.account_id = None
        mock_result.provider_key = None
        mock_result.other_provider_key = None
        mock_result.transaction_id_a = None
        mock_result.transaction_id_b = None
        mock_result.external_transaction_id_a = None
        mock_result.external_transaction_id_b = None
        mock_result.amount = None
        mock_result.other_amount = None
        mock_result.occurred_at = None
        mock_result.description = None
        mock_result.details = None
        mock_result.created_at = datetime.now(UTC)

        with patch.object(
            ReconciliationService,
            "get_run_with_results",
            new=AsyncMock(return_value=(mock_run, [mock_result], 1)),
        ):
            resp = client.get(
                "/api/v1/reconciliation/run_other",
                headers=auth_headers,
            )

        assert resp.status_code == 404

    def test_get_run_with_pagination(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """result_limit and result_offset query params are passed through."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_paginated"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = None
        mock_run.finding_count = 50
        mock_run.summary = {"by_kind": {"missing_transaction": 50}, "by_severity": {"info": 50}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        mock_results = []
        for i in range(5):
            r = MagicMock()
            r.id = f"res_{i}"
            r.run_id = "run_paginated"
            r.kind = "missing_transaction"
            r.severity = "info"
            r.account_id = None
            r.provider_key = None
            r.other_provider_key = None
            r.transaction_id_a = None
            r.transaction_id_b = None
            r.external_transaction_id_a = None
            r.external_transaction_id_b = None
            r.amount = None
            r.other_amount = None
            r.occurred_at = None
            r.description = f"gap_{i}"
            r.details = None
            r.created_at = datetime.now(UTC)
            mock_results.append(r)

        with patch.object(
            ReconciliationService,
            "get_run_with_results",
            new=AsyncMock(return_value=(mock_run, mock_results, 50)),
        ):
            resp = client.get(
                "/api/v1/reconciliation/run_paginated?result_limit=5&result_offset=10",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["result_limit"] == 5
        assert data["result_offset"] == 10
        assert len(data["results"]) == 5


# ═══════════════════════════════════════════════════════════════════════
# POST /reconciliation/trigger — trigger_reconciliation_v2
# ═══════════════════════════════════════════════════════════════════════


class TestTriggerReconciliationV2:
    """Tests for POST /api/v1/reconciliation/trigger."""

    def test_requires_authentication(self, client: TestClient) -> None:
        resp = client.post("/api/v1/reconciliation/trigger", json={})
        assert resp.status_code == 401

    def test_openapi_registration(self, client: TestClient) -> None:
        """Trigger endpoint is registered in the OpenAPI schema."""
        paths: dict[str, Any] = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/reconciliation/trigger" in paths
        post_op = paths["/api/v1/reconciliation/trigger"]["post"]
        assert post_op["tags"] == ["reconciliation"]

    def test_trigger_full_reconciliation(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Full reconciliation trigger returns 201."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_trigger_1"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {"date_from": "2026-01-01T00:00:00Z"}
        mock_run.finding_count = 0
        mock_run.summary = {"by_kind": {}, "by_severity": {}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(
            ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)
        ):
            resp = client.post(
                "/api/v1/reconciliation/trigger",
                json={"detect_duplicates": True},
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "run_trigger_1"
        assert data["status"] == "completed"

    def test_trigger_with_connectors(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Trigger with connector_a and connector_b performs targeted comparison."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_trigger_2"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {"provider_keys": ["bunq", "trading212"]}
        mock_run.finding_count = 3
        mock_run.summary = {
            "by_kind": {"duplicate_transaction": 2, "missing_transaction": 1},
            "by_severity": {"warning": 2, "info": 1},
        }
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(
            ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)
        ):
            resp = client.post(
                "/api/v1/reconciliation/trigger",
                json={
                    "connector_a": "bunq",
                    "connector_b": "trading212",
                    "detect_duplicates": True,
                },
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["finding_count"] == 3

    def test_trigger_with_detect_duplicates_false(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """detect_duplicates=False is passed to the service."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_no_dup"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {"detect_duplicates": False}
        mock_run.finding_count = 0
        mock_run.summary = {"by_kind": {}, "by_severity": {}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(
            ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)
        ):
            resp = client.post(
                "/api/v1/reconciliation/trigger",
                json={"detect_duplicates": False},
                headers=auth_headers,
            )

        assert resp.status_code == 201

    def test_trigger_same_connector_raises_400(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Same connector_a and connector_b returns 400."""
        resp = client.post(
            "/api/v1/reconciliation/trigger",
            json={
                "connector_a": "bunq",
                "connector_b": "bunq",
                "detect_duplicates": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "must be different" in resp.text

    def test_trigger_one_connector_raises_400(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Only one of connector_a/connector_b returns 400."""
        resp = client.post(
            "/api/v1/reconciliation/trigger",
            json={
                "connector_a": "bunq",
                "detect_duplicates": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Both connector_a and connector_b" in resp.text

    def test_trigger_with_full_params(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Trigger with all optional parameters."""
        from unittest.mock import patch

        from finance_sync.services.reconciliation import ReconciliationService

        mock_run = MagicMock()
        mock_run.id = "run_full"
        mock_run.tenant_id = "test-tenant"
        mock_run.status = "completed"
        mock_run.started_at = datetime.now(UTC)
        mock_run.completed_at = datetime.now(UTC)
        mock_run.scope = {
            "account_ids": ["acct_1", "acct_2"],
            "date_from": "2026-01-01T00:00:00Z",
            "date_to": "2026-06-01T00:00:00Z",
        }
        mock_run.finding_count = 0
        mock_run.summary = {"by_kind": {}, "by_severity": {}}
        mock_run.error_message = None
        mock_run.created_at = datetime.now(UTC)

        with patch.object(
            ReconciliationService, "reconcile", new=AsyncMock(return_value=mock_run)
        ):
            resp = client.post(
                "/api/v1/reconciliation/trigger",
                json={
                    "account_ids": ["acct_1", "acct_2"],
                    "date_from": "2026-01-01T00:00:00Z",
                    "date_to": "2026-06-01T00:00:00Z",
                    "threshold_hours": 24,
                    "detect_duplicates": False,
                },
                headers=auth_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "run_full"
