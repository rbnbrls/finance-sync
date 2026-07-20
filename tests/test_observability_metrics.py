"""Tests for the Prometheus metrics module."""

# pyright: basic
# prometheus-client lacks complete type stubs.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient

from finance_sync.app import create_app


@pytest.fixture
def app() -> FastAPI:
    """Build a test app with default (minimal) settings."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    """FastAPI test client that triggers lifespan events."""
    with TestClient(app) as c:
        yield c


class TestMetricsEndpoint:
    """GET /metrics — Prometheus scrape endpoint."""

    def test_metrics_returns_200(self, client: TestClient) -> None:
        response: Response = client.get("/metrics")
        assert response.status_code == 200

    def test_metrics_content_type(self, client: TestClient) -> None:
        """Prometheus metrics endpoint returns text/plain."""
        response: Response = client.get("/metrics")
        assert response.headers["content-type"].startswith(
            "text/plain"
        ) or response.headers["content-type"].startswith(
            "application/openmetrics-text"
        )

    def test_metrics_contains_expected_entries(
        self, client: TestClient
    ) -> None:
        """Known metric names appear in the /metrics output."""
        response: Response = client.get("/metrics")
        body: str = response.text
        # Core metrics
        assert (
            "http_requests_total" in body or "HELP http_requests_total" in body
        )
        assert "http_request_duration_seconds" in body
        assert "sync_runs_total" in body
        assert "transactions_ingested_total" in body

    def test_metrics_db_pool_gauges(self, client: TestClient) -> None:
        """Database pool gauges are declared."""
        response: Response = client.get("/metrics")
        body: str = response.text
        assert "db_pool_min" in body
        assert "db_pool_max" in body
        assert "db_pool_used" in body
        assert "db_pool_available" in body

    def test_request_records_metrics(self, client: TestClient) -> None:
        """A regular API request increments the HTTP counter."""
        # Make a request first
        client.get("/api/v1/")
        # Check that the counter was incremented
        response: Response = client.get("/metrics")
        body: str = response.text
        # We should see http_requests_total with our request details
        assert (
            "http_requests_total_total" in body or "http_requests_total" in body
        )
        assert "/api/v1/" in body

    def test_health_endpoint_does_not_increment_metrics(
        self, client: TestClient
    ) -> None:
        """Health check endpoints are excluded from metrics recording."""
        # Hit a health endpoint — should not add /health to http_requests_total
        client.get("/health")
        after = client.get("/metrics").text
        # The /health path should NOT appear as a label in http_requests_total
        # (health paths are excluded from Prometheus metric recording)
        assert '/health"' not in after
