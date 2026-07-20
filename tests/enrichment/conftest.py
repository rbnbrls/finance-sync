"""Shared test fixtures for enrichment module tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.models import PriceObservation, ResolvedSecurity


@pytest.fixture
def sample_resolved_security() -> ResolvedSecurity:
    """Return a sample resolved security."""
    return ResolvedSecurity(
        security_id="sec_uuid_abc123",
        isin="US0378331005",
        figi="BBG000B9XRY4",
        ticker="AAPL",
        name="Apple Inc.",
        currency_code="USD",
        confidence="exact",
        source="openbb",
    )


@pytest.fixture
def sample_price_observation() -> PriceObservation:
    """Return a sample price observation."""
    return PriceObservation(
        security_id="sec_uuid_abc123",
        timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
        price_open=Decimal("190.50"),
        price_high=Decimal("195.80"),
        price_low=Decimal("189.20"),
        price_close=Decimal("194.30"),
        volume=Decimal(45000000),
        source="openbb",
        interval="1d",
        currency_code="USD",
    )


@pytest.fixture
def sample_price_observations() -> list[PriceObservation]:
    """Return a list of sample price observations."""
    base = datetime(2025, 6, 10, 20, 0, 0, tzinfo=UTC)
    return [
        PriceObservation(
            security_id="sec_uuid_abc123",
            timestamp=base + (i * 86400),  # +i days
            price_open=Decimal(f"{190 + i}.50"),
            price_high=Decimal(f"{195 + i}.80"),
            price_low=Decimal(f"{189 + i}.20"),
            price_close=Decimal(f"{194 + i}.30"),
            volume=Decimal(45000000),
            source="openbb",
            interval="1d",
            currency_code="USD",
        )
        for i in range(5)
    ]


@pytest.fixture
def mock_settings():
    """Return mocked settings for enrichment tests."""
    settings = MagicMock()
    settings.openbb_api_key = None  # degraded mode
    settings.openbb_base_url = "https://openbb.co/api"
    settings.openbb_api_version = "v1"
    settings.openbb_request_timeout = 30
    settings.price_store_keep_minute_days = 30
    settings.price_store_keep_hour_days = 90
    settings.price_store_keep_daily_forever = True
    return settings


@pytest.fixture
def mock_uow():
    """Return a mock UnitOfWork with mock repositories."""
    uow = MagicMock()
    uow.securities = AsyncMock()
    uow.security_prices = AsyncMock()
    uow.enrichment_freshness = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.commit = AsyncMock()
    uow.rollback = AsyncMock()
    return uow
