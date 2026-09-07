"""Composition service for analytics built on canonical read models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select

from finance_sync.models import DetectedSubscription, MarketIntelligenceItem
from finance_sync.schemas.analytics import (
    AIAnalyticsSection,
    AnalyticsOverview,
    AnalyticsSection,
)
from finance_sync.schemas.freshness import (
    CoverageInfo,
    build_meta,
    freshness_for,
)
from finance_sync.services.performance import PerformanceService
from finance_sync.services.read_api import ReadService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.services.visibility import ReadScope


class AnalyticsOverviewService:
    """Compose existing analytics without creating a second status model."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        scope: ReadScope | None = None,
        now: datetime | None = None,
        ai_enabled: bool = False,
        ai_configured: bool = False,
    ) -> None:
        self._read = ReadService(session, scope=scope)
        self._performance = PerformanceService(session, scope=scope)
        self._session = session
        self._now = now or datetime.now(UTC)
        self._scope = scope
        self._ai_enabled = ai_enabled
        self._ai_configured = ai_configured

    async def get_overview(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        benchmark_security_id: str | None = None,
        include_details: bool = False,
    ) -> AnalyticsOverview:
        """Return portfolio, performance and cashflow in one typed response."""
        portfolio = await self._read.get_portfolio(tenant_id)
        performance = await self._performance.get_summary(
            tenant_id,
            date_from=date_from,
            date_to=date_to,
            benchmark_security_id=benchmark_security_id,
        )
        cashflow = await self._read.get_cashflow(
            tenant_id,
            date_from=date_from,
            date_to=date_to,
        )

        subscription_count, subscription_as_of = await self._section_stats(
            tenant_id,
            DetectedSubscription,
            DetectedSubscription.last_detected_at,
            account_scoped=True,
        )
        market_count, market_as_of = await self._section_stats(
            tenant_id,
            MarketIntelligenceItem,
            MarketIntelligenceItem.fetched_at,
        )
        subscription_details = (
            await self._subscription_details(tenant_id)
            if include_details
            else []
        )
        market_details = (
            await self._market_details(tenant_id) if include_details else []
        )

        subscription_freshness = freshness_for(
            subscription_as_of, now=self._now
        )
        market_freshness = freshness_for(market_as_of, now=self._now)
        metas = [performance.meta, cashflow.meta]
        section_meta = [
            (subscription_as_of, subscription_freshness),
            (market_as_of, market_freshness),
        ]
        timestamps = [meta.as_of for meta in metas if meta.as_of is not None]
        timestamps.extend(
            value for value, _ in section_meta if value is not None
        )
        as_of = max(timestamps, default=None)
        freshness_values = [meta.freshness for meta in metas]
        freshness_values.extend(value for _, value in section_meta)
        known = [value for value in freshness_values if value != "unknown"]
        freshness = (
            "unavailable"
            if not known
            else (
                "partial"
                if any(value != "fresh" for value in known)
                else "fresh"
            )
        )
        caveats = [caveat for meta in metas for caveat in meta.caveats]
        coverage = CoverageInfo(
            holdings=len(
                [
                    holding
                    for account in portfolio.accounts
                    for holding in account.holdings
                ]
            ),
            accounts=len(portfolio.accounts),
            items=cashflow.transaction_count,
        )
        aggregate_meta = build_meta(
            as_of=as_of,
            now=self._now,
            freshness=freshness,
            coverage=coverage,
            caveats=list(dict.fromkeys(caveats)),
        )
        return AnalyticsOverview(
            portfolio=portfolio,
            performance=performance,
            cashflow=cashflow,
            subscriptions=AnalyticsSection(
                items=subscription_count,
                as_of=subscription_as_of,
                freshness=subscription_freshness,
                coverage=CoverageInfo(items=subscription_count),
                caveats=(
                    ["Alleen subscriptions binnen de zichtbare accountscope."]
                    if self._scope is not None
                    and self._scope.account_ids is not None
                    else []
                ),
                details=subscription_details,
            ),
            market_intelligence=AnalyticsSection(
                items=market_count,
                as_of=market_as_of,
                freshness=market_freshness,
                coverage=CoverageInfo(items=market_count),
                caveats=[],
                details=market_details,
            ),
            ai_summary=AIAnalyticsSection(
                enabled=self._ai_enabled,
                configured=self._ai_configured,
                as_of=aggregate_meta.as_of,
                freshness=aggregate_meta.freshness,
                coverage=aggregate_meta.coverage or CoverageInfo(),
                caveats=(
                    []
                    if self._ai_enabled and self._ai_configured
                    else [
                        "AI-samenvatting wordt niet automatisch gegenereerd "
                        "zonder ingeschakelde provider en API-key."
                    ]
                ),
            ),
            meta=aggregate_meta,
            generated_at=self._now,
        )

    async def _subscription_details(
        self, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Return bounded, scope-safe subscription detail rows."""
        predicates = [DetectedSubscription.tenant_id == tenant_id]
        if self._scope is not None and self._scope.account_ids is not None:
            predicates.append(
                DetectedSubscription.account_id.in_(self._scope.account_ids)
            )
        rows = (
            await self._session.execute(
                select(DetectedSubscription)
                .where(*predicates)
                .order_by(DetectedSubscription.last_detected_at.desc())
                .limit(100)
            )
        ).scalars()
        return [
            {
                "id": str(row.id),
                "merchant_name": row.merchant_name,
                "amount": str(row.amount),
                "currency_code": row.currency_code,
                "frequency_days": row.frequency_days,
                "frequency_label": row.frequency_label,
                "confidence": str(row.confidence),
                "status": str(row.status),
                "account_id": str(row.account_id) if row.account_id else None,
                "provider_key": row.provider_key,
                "category": row.category,
                "first_detected_at": row.first_detected_at,
                "last_detected_at": row.last_detected_at,
                "occurrence_count": row.occurrence_count,
            }
            for row in rows
        ]

    async def _market_details(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return bounded provenance/detail rows without licensed body text."""
        rows = (
            await self._session.execute(
                select(MarketIntelligenceItem)
                .where(MarketIntelligenceItem.tenant_id == tenant_id)
                .order_by(MarketIntelligenceItem.published_at.desc())
                .limit(100)
            )
        ).scalars()
        return [
            {
                "id": str(row.id),
                "provider": row.provider,
                "source_id": row.source_id,
                "canonical_url": row.canonical_url,
                "kind": row.kind,
                "published_at": row.published_at,
                "fetched_at": row.fetched_at,
                "license_class": row.license_class,
                "headline": row.headline,
                "summary": row.summary,
                "facts": row.facts,
                "is_stale": row.is_stale,
            }
            for row in rows
        ]

    async def _section_stats(
        self,
        tenant_id: str,
        model: type[DetectedSubscription] | type[MarketIntelligenceItem],
        as_of_column: object,
        *,
        account_scoped: bool = False,
    ) -> tuple[int, datetime | None]:
        """Read count/as-of from canonical persisted analytics sources."""
        predicates = [model.tenant_id == tenant_id]
        if account_scoped and self._scope is not None:
            account_ids = self._scope.account_ids
            if account_ids is not None:
                predicates.append(model.account_id.in_(account_ids))  # type: ignore[attr-defined]
        count = int(
            await self._session.scalar(
                select(func.count()).select_from(model).where(*predicates)
            )
            or 0
        )
        as_of = cast(
            "datetime | None",
            await self._session.scalar(
                select(func.max(as_of_column))
                .select_from(model)
                .where(*predicates)
            ),
        )
        return count, as_of
