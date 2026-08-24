"""Contract tests for the phase-6 analytics consumer overview."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db
from finance_sync.services.analytics_overview import AnalyticsOverviewService
from finance_sync.services.performance import PerformanceSummaryResponse
from finance_sync.services.read_api import CashflowResponse, PortfolioResponse


def _settings() -> Settings:
    return Settings(
        environment="dev",
        secret_key="test-secret-key-at-least-16-chars",
        database_url=None,
        redis_url=None,
        debug=False,
    )


def test_overview_is_registered_and_requires_authentication() -> None:
    app = create_app(settings=_settings())
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/overview")
        assert response.status_code == 401
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/analytics/overview" in paths


@pytest.mark.asyncio
async def test_overview_composes_existing_consumers_with_shared_metadata() -> (
    None
):
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[0, None, 0, None])
    service = AnalyticsOverviewService(
        session,
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    service._read.get_portfolio = AsyncMock(
        return_value=PortfolioResponse(accounts=[], currency_code="EUR")
    )
    service._read.get_cashflow = AsyncMock(
        return_value=CashflowResponse(
            total_inflows=10,
            total_outflows=2,
            net_cashflow=8,
            transaction_count=2,
            currency_code="EUR",
        )
    )
    service._performance.get_summary = AsyncMock(
        return_value=PerformanceSummaryResponse(currency_code="EUR")
    )

    result = await service.get_overview("tenant-a")

    assert result.portfolio is not None
    assert result.cashflow is not None
    assert result.performance is not None
    assert result.meta.coverage is not None
    assert result.meta.coverage.items == 2
    assert result.subscriptions.items == 0
    assert result.market_intelligence.items == 0
    assert result.ai_summary.configured is False
    assert result.generated_at == datetime(2026, 1, 2, tzinfo=UTC)
    service._read.get_portfolio.assert_awaited_once_with("tenant-a")
    service._read.get_cashflow.assert_awaited_once()
    service._performance.get_summary.assert_awaited_once()
