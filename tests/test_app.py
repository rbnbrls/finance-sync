"""Tests for the FastAPI application factory."""

# pyright: basic
# The test file uses TestClient from httpx/fastapi which has incomplete
# type stubs.  Relaxed checking avoids noise from Unknown types.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from finance_sync.app import create_app
from finance_sync.config.settings import Settings

_TEST_SECRET: SecretStr = SecretStr("test-secret-key-at-least-16-chars")


@pytest.fixture
def app() -> FastAPI:
    """Build a test app with minimal settings (no DB/Redis)."""
    return create_app(
        settings=Settings(
            database_url=None,
            redis_url=None,
            secret_key=_TEST_SECRET,
        )
    )


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    """FastAPI test client that triggers lifespan events."""
    with TestClient(app) as c:
        yield c


class TestRootEndpoint:
    """GET /api/v1/ health check."""

    def test_root_returns_ok(self, client: TestClient) -> None:
        response: Response = client.get("/api/v1/")
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        assert data["status"] == "ok"
        assert data["version"] == "0.1.0"

    def test_root_content_type(self, client: TestClient) -> None:
        response: Response = client.get("/api/v1/")
        assert response.headers["content-type"] == "application/json"


class TestCORS:
    """CORS middleware is enabled."""

    def test_cors_headers(self, client: TestClient) -> None:
        response: Response = client.options(
            "/api/v1/",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # CORS preflight should return 200 with appropriate headers
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers


class TestAppFactory:
    """create_app() behaviour."""

    def test_custom_settings(self) -> None:
        """Custom settings override defaults."""
        from finance_sync.config.settings import Settings

        settings = Settings(app_version="2.0.0")  # type: ignore[call-arg]
        app = create_app(settings=settings)
        assert app.version == "2.0.0"

    def test_debug_mode_by_environment(self) -> None:
        """The dev environment enables debug mode on the app."""
        app = create_app()
        assert app.debug is True  # dev environment → debug=True

    def test_production_hides_debug(self) -> None:
        """Production environment disables debug."""
        from finance_sync.config.settings import Settings

        settings = Settings(environment="prod", _env_file=None)  # type: ignore[call-arg]
        app = create_app(settings=settings)
        assert app.debug is False

    def test_app_title(self, app: FastAPI) -> None:
        assert app.title == "finance-sync"

    def test_openapi_schema_present(self, client: TestClient) -> None:
        """The OpenAPI schema is available at /openapi.json."""
        response: Response = client.get("/openapi.json")
        assert response.status_code == 200
        schema: dict[str, Any] = response.json()
        assert schema["info"]["title"] == "finance-sync"
        assert schema["info"]["version"] == "0.1.0"
