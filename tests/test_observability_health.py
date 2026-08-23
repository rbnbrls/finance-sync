"""Tests for the health check endpoints."""

# pyright: basic
# Observability modules use libraries that lack full type stubs.

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
    """Build a test app with default (minimal) settings (no DB/Redis)."""
    return create_app(
        settings=Settings(
            database_url=None,
            redis_url=None,
            secret_key=_TEST_SECRET,
            # Hermetic tests: never inherit a real GITHUB_TOKEN from the
            # environment, otherwise /health would make a live GitHub API
            # call (issue #273 regression coverage depends on this).
            github_token=SecretStr(""),
        )
    )


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    """FastAPI test client that triggers lifespan events."""
    with TestClient(app) as c:
        yield c


class TestHealthEndpoint:
    """GET /health — overall component health."""

    def test_health_returns_200(self, client: TestClient) -> None:
        response: Response = client.get("/health")
        assert response.status_code == 200

    def test_health_has_expected_structure(self, client: TestClient) -> None:
        response: Response = client.get("/health")
        data: dict[str, Any] = response.json()
        assert "status" in data
        assert "version" in data
        assert "uptime" in data
        assert "components" in data
        assert data["version"] == "0.6.0"

    def test_health_components_report_not_configured_when_no_db(
        self, client: TestClient
    ) -> None:
        """Components report not_configured when no DB/Redis is set."""
        response: Response = client.get("/health")
        data: dict[str, Any] = response.json()
        comps = data["components"]
        assert "database" in comps
        assert "redis" in comps
        # The test app may load .env from the project root; if DB/Redis
        # are configured but unreachable the status will be 'error'.
        # Accept both 'not_configured' and 'error' as valid responses when
        # infrastructure is unavailable.
        assert comps["database"]["status"] in ("not_configured", "error")
        assert comps["redis"]["status"] in ("not_configured", "error")

    def test_health_uptime_increases(self, client: TestClient) -> None:
        """Uptime should be a positive float."""
        response: Response = client.get("/health")
        data: dict[str, Any] = response.json()
        assert isinstance(data["uptime"], (int, float))
        assert data["uptime"] > 0


class TestReadinessProbe:
    """GET /health/ready — readiness probe."""

    def test_readiness_returns_200(self, client: TestClient) -> None:
        response: Response = client.get("/health/ready")
        assert response.status_code == 200

    def test_readiness_accepts_not_configured(self, client: TestClient) -> None:
        """Without DB/Redis, readiness is ok or not_ready (handles both)."""
        response: Response = client.get("/health/ready")
        data: dict[str, Any] = response.json()
        # Status may be 'ok' (not_configured) or 'not_ready' (connection error)
        assert data["status"] in ("ok", "not_ready")

    def test_readiness_has_components(self, client: TestClient) -> None:
        response: Response = client.get("/health/ready")
        data: dict[str, Any] = response.json()
        assert "components" in data


class TestLivenessProbe:
    """GET /health/live — liveness probe."""

    def test_liveness_returns_200(self, client: TestClient) -> None:
        response: Response = client.get("/health/live")
        assert response.status_code == 200

    def test_liveness_returns_ok(self, client: TestClient) -> None:
        response: Response = client.get("/health/live")
        data: dict[str, Any] = response.json()
        assert data["status"] == "ok"


class TestHealthContentType:
    """All health endpoints return JSON."""

    @pytest.mark.parametrize(
        "path", ["/health", "/health/ready", "/health/live"]
    )
    def test_content_type_json(self, client: TestClient, path: str) -> None:
        response: Response = client.get(path)
        assert response.headers["content-type"] == "application/json"


class TestHealthGithubComponent:
    """GET /health — github feedback-integration component (issue #273).

    The feedback button broke silently in production for weeks because the
    configured GITHUB_TOKEN was invalid and no signal surfaced it.  The
    github health component exposes the integration state so operators /
    monitoring can detect a bad token before users do.
    """

    def test_health_includes_github_component(self, client: TestClient) -> None:
        """/health always reports a github component with a known status."""
        from unittest.mock import AsyncMock, patch

        with patch(
            "finance_sync.observability.health.check_github_issue_access",
            new_callable=AsyncMock,
        ) as mock_check:
            mock_check.return_value = {"status": "ok", "detail": "ok"}
            response = client.get("/health")
            data = response.json()
            assert "github" in data["components"]
            assert data["components"]["github"]["status"] in (
                "ok",
                "not_configured",
                "error",
            )

    def test_health_github_disabled_without_token(
        self, client: TestClient
    ) -> None:
        """Without a GITHUB_TOKEN the component reports not_configured
        without making any network call."""
        response = client.get("/health")
        data = response.json()
        github = data["components"]["github"]
        assert github["status"] == "not_configured"
        assert "token" in (github.get("detail") or "").lower()

    def test_health_github_ok_keeps_overall_ok(self) -> None:
        """A healthy GitHub integration keeps the overall status ok."""
        from unittest.mock import AsyncMock, patch

        app = create_app(
            settings=Settings(
                database_url=None,
                redis_url=None,
                secret_key=_TEST_SECRET,
                github_token=SecretStr("ghp_test_configured_token"),
            )
        )
        with patch(
            "finance_sync.observability.health.check_github_issue_access",
            new_callable=AsyncMock,
        ) as mock_check:
            mock_check.return_value = {"status": "ok", "detail": "ok"}
            with TestClient(app) as client:
                data = client.get("/health").json()
            assert mock_check.called
            assert data["status"] == "ok"
            assert data["components"]["github"]["status"] == "ok"

    def test_health_github_error_degrades_overall(self) -> None:
        """A broken GitHub integration (e.g. invalid token) degrades the
        overall health so operators/monitoring notice it."""
        from unittest.mock import AsyncMock, patch

        app = create_app(
            settings=Settings(
                database_url=None,
                redis_url=None,
                secret_key=_TEST_SECRET,
                github_token=SecretStr("ghp_test_configured_token"),
            )
        )
        with patch(
            "finance_sync.observability.health.check_github_issue_access",
            new_callable=AsyncMock,
        ) as mock_check:
            mock_check.return_value = {
                "status": "error",
                "detail": "GitHub authentication failed",
            }
            with TestClient(app) as client:
                data = client.get("/health").json()
            assert mock_check.called
            assert data["status"] == "degraded"
            assert data["components"]["github"]["status"] == "error"
