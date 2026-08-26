"""Unit-level HTTP contract tests for the control-plane routes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from finance_sync.api.deps.auth import (
    APIKeyAuthResult,
    AuthContext,
    get_auth_context,
)
from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db
from finance_sync.schemas.control_plane import ControlPlaneOverview
from finance_sync.schemas.data_health import DataHealthOverview
from finance_sync.schemas.data_quality import DataQualityOverview

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI


_SECRET = SecretStr("test-secret-key-at-least-16-chars")


def _auth() -> AuthContext:
    user = SimpleNamespace(id="user-a", tenant_id="tenant-a", role="admin")
    return AuthContext(user=user)


@pytest.fixture
def app() -> FastAPI:
    application = create_app(
        settings=Settings(
            database_url=None,
            redis_url=None,
            secret_key=_SECRET,
            _env_file=None,
        )
    )
    application.dependency_overrides[get_auth_context] = _auth
    application.dependency_overrides[get_db] = lambda: object()
    return application


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


def _overview() -> ControlPlaneOverview:
    return ControlPlaneOverview(
        status="healthy",
        installation={"redis": "not_configured"},
        summary={},
        connections=[],
        syncs=[],
        issues=[],
        freshness={"status": "unavailable"},
        coverage={},
        destinations=[],
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def _quality() -> DataQualityOverview:
    return DataQualityOverview(
        status="unavailable",
        findings_total=0,
        findings_by_kind={},
        coverage=[],
        issues=[],
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def _data_health() -> DataHealthOverview:
    return DataHealthOverview(
        status="healthy",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def test_control_plane_routes_are_registered(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/control-plane/overview" in paths
    assert "/api/v1/control-plane/data-quality" in paths
    assert "/api/v1/control-plane/data-health" in paths
    assert "/api/v1/control-plane/provider-health" in paths


def test_data_health_route_passes_tenant_permissions_and_redis_state(
    client: TestClient,
) -> None:
    service = AsyncMock()
    service.return_value = _data_health()
    container = SimpleNamespace(
        settings=SimpleNamespace(redis_url="redis://test")
    )

    with (
        patch(
            "finance_sync.api.v1.control_plane.get_container",
            return_value=container,
        ),
        patch(
            "finance_sync.api.v1.control_plane.DataHealthService",
            return_value=SimpleNamespace(get_overview=service),
        ) as service_factory,
    ):
        response = client.get("/api/v1/control-plane/data-health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    args, kwargs = service_factory.call_args
    assert args[1] == "tenant-a"
    assert kwargs["redis_configured"] is True
    service.assert_awaited_once_with()


def test_overview_route_passes_tenant_permissions_and_redis_state(
    client: TestClient,
) -> None:
    service = AsyncMock()
    service.return_value = _overview()
    container = SimpleNamespace(
        settings=SimpleNamespace(redis_url="redis://test"),
    )

    with (
        patch(
            "finance_sync.api.v1.control_plane.get_container",
            return_value=container,
        ),
        patch(
            "finance_sync.api.v1.control_plane.ControlPlaneService",
            return_value=SimpleNamespace(get_overview=service),
        ) as service_factory,
    ):
        response = client.get("/api/v1/control-plane/overview")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    service_factory.assert_called_once()
    args, kwargs = service_factory.call_args
    assert args[1] == "tenant-a"
    assert kwargs["redis_configured"] is True
    service.assert_awaited_once_with()


def test_data_quality_route_returns_typed_service_result(
    client: TestClient,
) -> None:
    service = AsyncMock()
    service.return_value = _quality()

    with patch(
        "finance_sync.api.v1.control_plane.DataQualityService",
        return_value=SimpleNamespace(get_overview=service),
    ) as service_factory:
        response = client.get("/api/v1/control-plane/data-quality")

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    service_factory.assert_called_once_with(ANY, "tenant-a")
    service.assert_awaited_once_with()


def test_control_plane_requires_authentication() -> None:
    application = create_app(
        settings=Settings(
            database_url=None,
            redis_url=None,
            secret_key=_SECRET,
            _env_file=None,
        )
    )
    application.dependency_overrides[get_db] = lambda: object()

    with TestClient(application) as unauthenticated:
        response = unauthenticated.get("/api/v1/control-plane/overview")

    assert response.status_code == 401


def test_control_plane_rejects_api_key_without_read_permission() -> None:
    application = create_app(
        settings=Settings(
            database_url=None,
            redis_url=None,
            secret_key=_SECRET,
            _env_file=None,
        )
    )
    application.dependency_overrides[get_auth_context] = lambda: AuthContext(
        api_key_result=APIKeyAuthResult(
            tenant_id="tenant-a", permissions="connectors:read"
        )
    )
    application.dependency_overrides[get_db] = lambda: object()

    with TestClient(application) as read_only:
        response = read_only.get("/api/v1/control-plane/overview")

    assert response.status_code == 403
    assert "sync:read" in response.json()["detail"]
