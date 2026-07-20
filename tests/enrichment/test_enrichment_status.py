"""Tests for the GET /enrichment/status endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestEnrichmentStatusEndpoint:
    """Tests for the enrichment status endpoint."""

    @pytest.fixture
    def mock_container(self):
        container = MagicMock()
        container.settings = MagicMock()
        return container

    async def test_endpoint_registered(self) -> None:
        """Enrichment router is registered in the app."""
        from finance_sync.api.v1.enrichment import router

        routes = [r.path for r in router.routes]
        assert "/enrichment/status" in routes

    async def test_endpoint_method(self) -> None:
        """Endpoint uses GET method."""
        from finance_sync.api.v1.enrichment import router

        for route in router.routes:
            if route.path == "/enrichment/status":
                methods = route.methods
                assert methods and "GET" in methods
                return

    async def test_status_summary_model(self) -> None:
        """EnrichmentStatusSummary model has correct fields."""
        from finance_sync.enrichment.models import EnrichmentStatusSummary

        summary = EnrichmentStatusSummary(
            total_securities=100,
            enriched_securities=75,
            pending_securities=20,
            failed_securities=5,
            stale_securities=10,
            last_enrichment_run=None,
            data_sources=["openbb"],
        )
        assert summary.total_securities == 100
        assert summary.enriched_securities == 75
        assert summary.pending_securities == 20
        assert summary.failed_securities == 5
        assert summary.stale_securities == 10
        assert summary.data_sources == ["openbb"]
