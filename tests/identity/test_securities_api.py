"""Integration tests for the securities identity resolution API endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from finance_sync.app import create_app
from finance_sync.config.environments import Environment


@pytest.fixture
def settings_override():
    """Override settings for API tests."""
    settings = MagicMock()
    settings.environment = Environment.DEVELOPMENT
    settings.app_name = "finance-sync-test"
    settings.database_url = None
    settings.redis_url = None
    settings.debug = True
    settings.is_debug = True
    settings.secret_key = "test-secret-key-not-for-prod"
    settings.access_token_expire_minutes = 30
    settings.refresh_token_expire_days = 7
    settings.jwt_algorithm = "HS256"
    settings.openbb_api_key = None
    settings.openbb_base_url = "https://openbb.co/api"
    settings.openbb_api_version = "v1"
    settings.openbb_request_timeout = 30
    settings.openbb_rate_limit_rps = 10
    settings.price_store_keep_minute_days = 30
    settings.price_store_keep_hour_days = 90
    settings.price_store_keep_daily_forever = True
    return settings


@pytest.fixture
def mock_container(settings_override):
    """Create a mock container with mocked identity resolution service."""
    container = MagicMock()
    container.settings = settings_override
    container._settings = settings_override

    mock_identity_service = MagicMock()
    mock_identity_service.get_unresolved = AsyncMock()
    mock_identity_service.manually_resolve = AsyncMock()
    mock_identity_service.map_and_resolve = AsyncMock()
    mock_identity_service.get_audit_log = AsyncMock()
    container.identity_resolution_service = mock_identity_service

    return container


@pytest.fixture
def client(mock_container):
    """Create a test client with a mocked container."""
    # Bypass the lifespan to avoid container overwrite
    with patch("finance_sync.app.lifespan", None):
        app = create_app()
    app.state.container = mock_container
    with TestClient(app) as c:
        yield c


class TestListUnresolved:
    """Tests for GET /securities/unresolved."""

    def test_returns_unresolved_list(self, client, mock_container):
        """GET /securities/unresolved lists unresolved securities."""
        from finance_sync.models.unresolved_security import UnresolvedSecurity

        unresolved_mock = MagicMock(spec=UnresolvedSecurity)
        unresolved_mock.id = "unres_001"
        unresolved_mock.provider_key = "trading212"
        unresolved_mock.external_security_id = "EQ.US0378331005"
        unresolved_mock.raw_isin = "US0378331005"
        unresolved_mock.raw_figi = "EQ.US0378331005"
        unresolved_mock.raw_ticker = None
        unresolved_mock.raw_name = "Apple Inc."
        unresolved_mock.raw_currency_code = "USD"
        unresolved_mock.raw_metadata = None
        unresolved_mock.resolved_security_id = None
        unresolved_mock.resolution_method = None
        unresolved_mock.resolution_notes = None
        unresolved_mock.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        unresolved_mock.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

        svc = mock_container.identity_resolution_service
        svc.get_unresolved.return_value = [unresolved_mock]

        response = client.get("/api/v1/securities/unresolved")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == "unres_001"
        assert data["items"][0]["provider_key"] == "trading212"

    def test_returns_empty_list(self, client, mock_container):
        """GET /securities/unresolved empty when none exist."""
        svc = mock_container.identity_resolution_service
        svc.get_unresolved.return_value = []

        response = client.get("/api/v1/securities/unresolved")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 0
        assert data["total"] == 0

    def test_filters_by_provider(self, client, mock_container):
        """GET /securities/unresolved filters by provider_key."""
        mock_container.identity_resolution_service.get_unresolved.return_value = []  # noqa: E501

        response = client.get(
            "/api/v1/securities/unresolved",
            params={"provider_key": "trading212"},
        )
        assert response.status_code == 200

        # Verify the service was called with the right provider_key
        mock_container.identity_resolution_service.get_unresolved.assert_called_with(
            only_unmapped=True,
            provider_key="trading212",
            limit=100,
            offset=0,
        )


class TestListAllUnresolved:
    """Tests for GET /securities/unresolved/all."""

    def test_includes_resolved(self, client, mock_container):
        """GET /securities/unresolved/all includes resolved entries."""
        mock_container.identity_resolution_service.get_unresolved.return_value = []  # noqa: E501

        response = client.get("/api/v1/securities/unresolved/all")
        assert response.status_code == 200

        # Verify only_unmapped=False
        mock_container.identity_resolution_service.get_unresolved.assert_called_with(
            only_unmapped=False,
            provider_key=None,
            limit=100,
            offset=0,
        )


class TestResolve:
    """Tests for POST /securities/resolve."""

    def test_resolves_successfully(self, client, mock_container):
        """POST /securities/resolve successfully resolves a security."""
        from finance_sync.models.resolution_audit_log import ResolutionAuditLog

        audit_mock = MagicMock(spec=ResolutionAuditLog)
        audit_mock.id = "audit_001"
        audit_mock.target_security_id = "sec_target_1"
        audit_mock.resolution_method = "manual"
        audit_mock.unresolved_security_id = "unres_001"
        audit_mock.resolver_principal = "api:user"
        audit_mock.resolved_at = datetime.now(UTC)

        mock_container.identity_resolution_service.manually_resolve.return_value = audit_mock  # noqa: E501

        response = client.post(
            "/api/v1/securities/resolve",
            json={
                "unresolved_security_id": "unres_001",
                "target_security_id": "sec_target_1",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "audit_001"
        assert data["target_security_id"] == "sec_target_1"
        assert "background enrichment triggered" in data["detail"]

    def test_returns_404_for_nonexistent(self, client, mock_container):
        """POST /securities/resolve returns 404 when not found."""
        mock_container.identity_resolution_service.manually_resolve.return_value = None  # noqa: E501

        response = client.post(
            "/api/v1/securities/resolve",
            json={
                "unresolved_security_id": "nonexistent",
                "target_security_id": "sec_target_1",
            },
        )
        assert response.status_code == 404

    def test_validates_request_body(self, client):
        """POST /securities/resolve validates required fields."""
        response = client.post(
            "/api/v1/securities/resolve",
            json={},
        )
        assert response.status_code == 422  # Validation error

    def test_accepts_optional_notes(self, client, mock_container):
        """POST /securities/resolve accepts optional resolution_notes."""
        from finance_sync.models.resolution_audit_log import ResolutionAuditLog

        audit_mock = MagicMock(spec=ResolutionAuditLog)
        audit_mock.id = "audit_001"
        audit_mock.target_security_id = "sec_target_1"
        audit_mock.resolution_method = "manual"

        mock_container.identity_resolution_service.manually_resolve.return_value = audit_mock  # noqa: E501

        response = client.post(
            "/api/v1/securities/resolve",
            json={
                "unresolved_security_id": "unres_001",
                "target_security_id": "sec_target_1",
                "resolution_notes": "Test notes",
            },
        )
        assert response.status_code == 200


class TestMap:
    """Tests for PUT /securities/map."""

    def test_maps_successfully(self, client, mock_container):
        """PUT /securities/map maps an incoming security to a canonical one."""
        from finance_sync.models.resolution_audit_log import ResolutionAuditLog

        audit_mock = MagicMock(spec=ResolutionAuditLog)
        audit_mock.target_security_id = "sec_target_1"

        svc = mock_container.identity_resolution_service
        svc.map_and_resolve.return_value = audit_mock

        response = client.put(
            "/api/v1/securities/map",
            json={
                "provider_key": "trading212",
                "external_security_id": "EQ.US0378331005",
                "target_security_id": "sec_target_1",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["target_security_id"] == "sec_target_1"
        assert data["provider_key"] == "trading212"

    def test_returns_404_for_missing_target(self, client, mock_container):
        """PUT /securities/map returns 404 when target not found."""
        mock_container.identity_resolution_service.map_and_resolve.return_value = None  # noqa: E501

        response = client.put(
            "/api/v1/securities/map",
            json={
                "provider_key": "trading212",
                "external_security_id": "EQ.US0378331005",
                "target_security_id": "nonexistent",
            },
        )
        assert response.status_code == 404


class TestAuditLog:
    """Tests for GET /securities/audit-log."""

    def test_returns_audit_log(self, client, mock_container):
        """GET /securities/audit-log returns audit entries."""
        from finance_sync.models.resolution_audit_log import ResolutionAuditLog

        audit_mock = MagicMock(spec=ResolutionAuditLog)
        audit_mock.id = "audit_001"
        audit_mock.unresolved_security_id = "unres_001"
        audit_mock.source_security_id = "US0378331005"
        audit_mock.target_security_id = "sec_001"
        audit_mock.resolution_method = "auto_isin"
        audit_mock.confidence = "exact"
        audit_mock.resolver_principal = "system"
        audit_mock.resolved_at = datetime(2025, 1, 1, tzinfo=UTC)
        audit_mock.resolution_detail = "Auto-resolved"
        audit_mock.match_score = None
        audit_mock.created_at = datetime(2025, 1, 1, tzinfo=UTC)

        svc = mock_container.identity_resolution_service
        svc.get_audit_log.return_value = [audit_mock]

        response = client.get("/api/v1/securities/audit-log")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == "audit_001"
        assert data["items"][0]["resolution_method"] == "auto_isin"

    def test_returns_empty_when_no_logs(self, client, mock_container):
        """GET /securities/audit-log returns empty list."""
        mock_container.identity_resolution_service.get_audit_log.return_value = []  # noqa: E501

        response = client.get("/api/v1/securities/audit-log")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 0

    def test_filters_by_target_security(self, client, mock_container):
        """GET /securities/audit-log filters by target_security_id."""
        mock_container.identity_resolution_service.get_audit_log.return_value = []  # noqa: E501

        response = client.get(
            "/api/v1/securities/audit-log",
            params={"target_security_id": "sec_001"},
        )
        assert response.status_code == 200
        mock_container.identity_resolution_service.get_audit_log.assert_called_with(
            target_security_id="sec_001",
            limit=100,
        )
