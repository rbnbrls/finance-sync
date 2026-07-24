"""Pydantic DTOs for enrichment services."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

# ── Security Resolution ─────────────────────────────────────────────────


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
    provider_metadata: dict[str, Any] | None = Field(
        default=None,
        description="Provider-specific additional resolution metadata",
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


class FxRateObservation(BaseModel):
    """A single exchange rate observation."""

    base_currency: str = Field(
        description="ISO-4217 base currency code (e.g. 'EUR')"
    )
    quote_currency: str = Field(
        description="ISO-4217 quote currency code (e.g. 'USD')"
    )
    rate: Decimal = Field(description="Exchange rate value")
    timestamp: datetime = Field(
        description="When the rate observation was recorded"
    )
    source: str = Field(default="openbb", description="Data source identifier")

    def inverse(self) -> FxRateObservation:
        """Return the inverse rate observation (quote → base)."""
        return FxRateObservation(
            base_currency=self.quote_currency,
            quote_currency=self.base_currency,
            rate=round(Decimal(1) / self.rate, 12),
            timestamp=self.timestamp,
            source=self.source,
        )


class FxConversionRequest(BaseModel):
    """Request to convert an amount from one currency to another."""

    from_currency: str = Field(
        description="Source ISO-4217 currency code",
    )
    to_currency: str = Field(
        description="Target ISO-4217 currency code",
    )
    amount: Decimal = Field(description="Amount to convert")
    at_timestamp: datetime | None = Field(
        default=None,
        description="Optional timestamp for historical rate lookup",
    )


class FxConversionResult(BaseModel):
    """Result of a currency conversion."""

    from_currency: str = Field(description="Source currency code")
    to_currency: str = Field(description="Target currency code")
    original_amount: Decimal = Field(description="Amount before conversion")
    converted_amount: Decimal = Field(description="Amount after conversion")
    rate_used: Decimal = Field(description="Exchange rate applied")
    rate_timestamp: datetime = Field(
        description="When the used rate was observed"
    )
    source: str = Field(description="Data source of the rate")


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


# ── Fundamentals ────────────────────────────────────────────────────────


class FundamentalObservationData(BaseModel):
    """Point-in-time fundamental ratio observation DTO."""

    security_id: str = Field(description="Canonical security ID")
    timestamp: datetime = Field(
        description="When the fundamental data was observed"
    )

    pe_ratio: Decimal | None = Field(
        default=None, description="Price-to-Earnings ratio (TTM)"
    )
    forward_pe: Decimal | None = Field(
        default=None, description="Forward Price-to-Earnings ratio"
    )
    peg_ratio: Decimal | None = Field(
        default=None, description="PE / Growth ratio"
    )
    eps: Decimal | None = Field(
        default=None, description="Earnings Per Share (TTM)"
    )
    eps_forward: Decimal | None = Field(
        default=None, description="Forward EPS estimate"
    )
    book_value_per_share: Decimal | None = Field(
        default=None, description="Book Value Per Share"
    )
    dividend_yield: Decimal | None = Field(
        default=None, description="Dividend yield (e.g. 0.035 = 3.5%)"
    )
    dividend_rate: Decimal | None = Field(
        default=None, description="Annual dividend per share"
    )
    market_cap: Decimal | None = Field(
        default=None, description="Market capitalisation in base currency"
    )
    enterprise_value: Decimal | None = Field(
        default=None, description="Enterprise value"
    )
    shares_outstanding: Decimal | None = Field(
        default=None, description="Shares outstanding"
    )
    beta: Decimal | None = Field(
        default=None, description="Beta (5-year monthly)"
    )
    high_52w: Decimal | None = Field(default=None, description="52-week high")
    low_52w: Decimal | None = Field(default=None, description="52-week low")
    source: str = Field(default="openbb", description="Data source")

    provider_metadata: dict[str, Any] | None = Field(default=None)


class FundamentalRatioSummary(BaseModel):
    """Condensed summary of key fundamental ratios for a security."""

    pe_ratio: Decimal | None = Field(
        default=None, description="Price-to-Earnings ratio (TTM)"
    )
    forward_pe: Decimal | None = Field(
        default=None, description="Forward Price-to-Earnings ratio"
    )
    dividend_yield: Decimal | None = Field(
        default=None, description="Dividend yield"
    )
    eps: Decimal | None = Field(
        default=None, description="Earnings Per Share (TTM)"
    )
    market_cap: Decimal | None = Field(
        default=None, description="Market capitalisation"
    )
    beta: Decimal | None = Field(
        default=None, description="Beta (5-year monthly)"
    )


# ── Security Metadata Observations ──────────────────────────────────────


class SecurityMetadataObservationData(BaseModel):
    """A point-in-time DTO for structured security metadata.

    The ``metadata_type`` discriminator determines what the
    ``metadata_json`` payload contains.
    """

    security_id: str = Field(description="Canonical security ID")
    metadata_type: str = Field(
        description=(
            "Discriminator: etf_composition, sector_exposure, "
            "fundamental_ratios, company_profile"
        )
    )
    timestamp: datetime = Field(description="When the metadata was observed")
    metadata_json: dict[str, Any] = Field(
        description="Arbitrary structured metadata payload"
    )
    label: str | None = Field(
        default=None,
        description="Human-readable label (e.g. ETF name, sector title)",
    )
    source: str = Field(default="openbb", description="Data source")


# ── ETF Composition ─────────────────────────────────────────────────────


class ETFHolding(BaseModel):
    """A single holding within an ETF portfolio."""

    ticker: str | None = Field(default=None, description="Holding ticker")
    name: str | None = Field(default=None, description="Holding name")
    weight: Decimal | None = Field(
        default=None, description="Allocation weight (e.g. 0.05 = 5%)"
    )
    sector: str | None = Field(
        default=None, description="Holding sector classification"
    )
    market_value: Decimal | None = Field(
        default=None, description="Market value in base currency"
    )
    shares: Decimal | None = Field(
        default=None, description="Number of shares held"
    )


class SectorExposure(BaseModel):
    """Sector weight within a portfolio or ETF."""

    sector: str = Field(description="Sector name (e.g. 'Technology')")
    weight: Decimal = Field(description="Allocation weight (e.g. 0.25 = 25%)")


class RegionExposure(BaseModel):
    """Geographic region weight within a portfolio or ETF."""

    region: str = Field(
        description="Region name (e.g. 'North America', 'Europe')"
    )
    weight: Decimal = Field(description="Allocation weight")


class ETFComposition(BaseModel):
    """Full composition breakdown for an ETF."""

    etf_name: str = Field(description="ETF name")
    total_holdings: int | None = Field(
        default=None, description="Number of holdings"
    )
    holdings: list[ETFHolding] = Field(
        default_factory=list, description="Top holdings"
    )
    sector_exposures: list[SectorExposure] = Field(
        default_factory=list, description="Sector allocation"
    )
    region_exposures: list[RegionExposure] = Field(
        default_factory=list, description="Geographic allocation"
    )
    expense_ratio: Decimal | None = Field(
        default=None, description="Expense ratio (e.g. 0.002 = 0.2%)"
    )
    dividend_yield: Decimal | None = Field(
        default=None, description="Dividend yield"
    )
    source: str = Field(default="openbb")


# Rebuild models to resolve forward references caused by
# ``from __future__ import annotations`` with Pydantic v2.
ResolvedSecurity.model_rebuild()
UnresolvedSecurity.model_rebuild()
QuoteResult.model_rebuild()
HistoricalPriceRequest.model_rebuild()
PriceObservation.model_rebuild()
FxRateObservation.model_rebuild()
FxConversionRequest.model_rebuild()
FxConversionResult.model_rebuild()
EnrichmentStatusSummary.model_rebuild()
FundamentalObservationData.model_rebuild()
FundamentalRatioSummary.model_rebuild()
SecurityMetadataObservationData.model_rebuild()
ETFHolding.model_rebuild()
SectorExposure.model_rebuild()
RegionExposure.model_rebuild()
ETFComposition.model_rebuild()
