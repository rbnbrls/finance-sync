"""Typed aggregate contract for the analytics consumer layer."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from finance_sync.schemas.freshness import AggregateMeta, CoverageInfo
from finance_sync.services.performance import PerformanceSummaryResponse
from finance_sync.services.read_api import CashflowResponse, PortfolioResponse


class AnalyticsSection(BaseModel):
    """Shared metadata envelope for a secondary analytics section."""

    items: int = 0
    as_of: datetime | None = None
    freshness: str = "unknown"
    coverage: CoverageInfo = Field(default_factory=CoverageInfo)
    caveats: list[str] = Field(default_factory=list)


class AIAnalyticsSection(BaseModel):
    """Availability metadata without generating an AI response implicitly."""

    enabled: bool = False
    configured: bool = False
    freshness: str = "unknown"
    caveats: list[str] = Field(default_factory=list)


class AnalyticsOverview(BaseModel):
    """A scope-safe composition of existing canonical-data analytics."""

    portfolio: PortfolioResponse | None = None
    performance: PerformanceSummaryResponse | None = None
    cashflow: CashflowResponse | None = None
    subscriptions: AnalyticsSection = Field(default_factory=AnalyticsSection)
    market_intelligence: AnalyticsSection = Field(
        default_factory=AnalyticsSection
    )
    ai_summary: AIAnalyticsSection = Field(default_factory=AIAnalyticsSection)
    meta: AggregateMeta = Field(default_factory=AggregateMeta)
    generated_at: datetime
