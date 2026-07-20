"""Pydantic DTOs for enrichment services."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from decimal import Decimal  # noqa: TC003
from typing import Any

from pydantic import BaseModel, Field


class ResolvedSecurity(BaseModel):
    """Result of a successful security resolution."""

    security_id: str = Field(description="Canonical security ID")
    isin: str | None = Field(default=None, description="Resolved ISIN")
    figi: str | None = Field(default=None, description="OpenFIGI identifier")
    ticker: str | None = Field(default=None, description="Ticker symbol")
    name: str = Field(description="Canonical security name")
    currency_code: str = Field(default="EUR", description="ISO-4217 currency")
    confidence: str = Field(
        default="high",
        description="Resolution confidence: exact/ticker_only/inferred/manual",
    )
    source: str = Field(
        default="openbb",
        description="Data source that resolved the security",
    )


class UnresolvedSecurity(BaseModel):
    """A security that could not be resolved automatically."""

    identifier: str = Field(description="The identifier that was searched")
    identifier_type: str = Field(
        description="Type: figi/ticker/name/isim/external_id"
    )
    reason: str = Field(description="Why resolution failed")
    provider_key: str | None = Field(
        default=None, description="Connector provider key"
    )


class QuoteResult(BaseModel):
    """A real-time or latest quote for a security."""

    security_id: str = Field(description="Canonical security ID")
    symbol: str | None = Field(default=None, description="Ticker symbol")
    price: Decimal = Field(description="Latest price")
    change: Decimal | None = Field(default=None, description="Price change")
    change_pct: Decimal | None = Field(
        default=None, description="Price change percentage"
    )
    currency_code: str = Field(default="EUR", description="ISO-4217")
    timestamp: datetime = Field(description="When the quote was observed")
    source: str = Field(default="openbb")


class HistoricalPriceRequest(BaseModel):
    """Request parameters for historical price fetch."""

    security_id: str
    interval: str = Field(
        default="1d",
        pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1w|1mo)$",
        description="Candle interval",
    )
    start_date: datetime | None = Field(default=None)
    end_date: datetime | None = Field(default=None)
    limit: int | None = Field(default=100, ge=1, le=1000)


class PriceObservation(BaseModel):
    """Normalised price observation from any source."""

    security_id: str
    timestamp: datetime
    price_open: Decimal | None = Field(default=None)
    price_high: Decimal | None = Field(default=None)
    price_low: Decimal | None = Field(default=None)
    price_close: Decimal | None = Field(default=None)
    volume: Decimal | None = Field(default=None)
    source: str = Field(default="openbb")
    interval: str = Field(default="1d")
    currency_code: str = Field(default="EUR")
    provider_metadata: dict[str, Any] | None = Field(default=None)


class EnrichmentStatusSummary(BaseModel):
    """Summary of enrichment coverage and freshness."""

    total_securities: int = Field(description="Total securities tracked")
    enriched_securities: int = Field(
        description="Securities with at least one price observation"
    )
    pending_securities: int = Field(
        description="Securities awaiting first enrichment"
    )
    failed_securities: int = Field(
        description="Securities where enrichment failed"
    )
    stale_securities: int = Field(
        description="Securities not enriched in the last 24 hours"
    )
    last_enrichment_run: datetime | None = Field(
        default=None, description="Timestamp of last enrichment activity"
    )
    data_sources: list[str] = Field(
        default_factory=list, description="Active data sources"
    )


# Rebuild models to resolve forward references caused by
# ``from __future__ import annotations`` with Pydantic v2.
ResolvedSecurity.model_rebuild()
UnresolvedSecurity.model_rebuild()
QuoteResult.model_rebuild()
HistoricalPriceRequest.model_rebuild()
PriceObservation.model_rebuild()
EnrichmentStatusSummary.model_rebuild()
