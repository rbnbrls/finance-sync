"""Pydantic DTOs for enrichment services."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

# ── Security Resolution ─────────────────────────────────────────────────


# ── Security Resolution ─────────────────────────────────────────────────


class ResolvedSecurity(BaseModel):
    """Result of a security identifier resolution."""

    security_id: str
    name: str
    ticker: str | None = None
    asset_type: str | None = None
    source: str
    identifier: str
    identifier_type: str
    resolved_at: datetime | None = None
    match_quality: float | None = None


class SectorObservation(BaseModel):
    """Sector classification for a security."""

    sector: str
    weight: float | None = None


# ── ETF Metadata ────────────────────────────────────────────────────────


class ETFComposition(BaseModel):
    """Composition breakdown for an ETF."""

    etf_name: str
    total_holdings: int = 0
    holdings: list[ETFHolding] = []
    sector_exposures: list[SectorExposure] = []
    region_exposures: list[RegionExposure] = []
    expense_ratio: Decimal | None = None


class ETFHolding(BaseModel):
    """Individual holding within an ETF."""

    symbol: str
    name: str | None = None
    weight: float | None = None
    market_value: float | None = None
    shares: float | None = None


class SectorExposure(BaseModel):
    """Sector exposure breakdown for an ETF."""

    sector: str
    weight: float | None = None


class RegionExposure(BaseModel):
    """Region exposure breakdown for an ETF."""

    region: str
    weight: float | None = None


# ── Fundamental Data ────────────────────────────────────────────────────


class FundamentalObservationData(BaseModel):
    """Point-in-time fundamental metrics for a security."""

    pe_ratio: Decimal | None = None
    forward_pe: Decimal | None = None
    peg_ratio: Decimal | None = None
    price_to_book: Decimal | None = None
    price_to_sales: Decimal | None = None
    enterprise_value: Decimal | None = None
    market_cap: Decimal | None = None
    eps: Decimal | None = None
    dividend_yield: Decimal | None = None
    dividend_rate: Decimal | None = None
    payout_ratio: Decimal | None = None
    beta: Decimal | None = None
    trailing_eps: Decimal | None = None
    forward_eps: Decimal | None = None
    revenue: Decimal | None = None
    revenue_growth: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    net_margin: Decimal | None = None
    return_on_equity: Decimal | None = None
    return_on_assets: Decimal | None = None
    debt_to_equity: Decimal | None = None
    current_ratio: Decimal | None = None
    free_cash_flow: Decimal | None = None
    free_cash_flow_yield: Decimal | None = None
    earnings_growth: Decimal | None = None
    revenue_per_share: Decimal | None = None
    book_value_per_share: Decimal | None = None
    held_percent: Decimal | None = None
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    fifty_two_week_change: Decimal | None = None


class FundamentalRatioSummary(BaseModel):
    """Human-readable summary of the most recent fundamental data."""

    pe_ratio: Decimal | None = None
    forward_pe: Decimal | None = None
    peg_ratio: Decimal | None = None
    price_to_book: Decimal | None = None
    price_to_sales: Decimal | None = None
    market_cap: Decimal | None = None
    eps: Decimal | None = None
    dividend_yield: Decimal | None = None
    beta: Decimal | None = None
    revenue: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    net_margin: Decimal | None = None
    return_on_equity: Decimal | None = None
    return_on_assets: Decimal | None = None
    debt_to_equity: Decimal | None = None
    earnings_growth: Decimal | None = None
    free_cash_flow: Decimal | None = None
    reported_at: datetime | None = None


class SecurityMetadataObservationData(BaseModel):
    """Metadata observation for a security enriched via OpenBB.

    Captures the identity-resolution metadata produced during an enrichment
    run (name, ticker, type, sector, industry, etc.).
    """

    security_id: str
    name: str | None = None
    ticker: str | None = None
    asset_type: str | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    country: str | None = None
    logo_url: str | None = None
    observed_at: datetime
