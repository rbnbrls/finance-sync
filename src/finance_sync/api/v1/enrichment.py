"""Enrichment status endpoint — coverage, freshness, and health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from finance_sync.dependencies import get_db
from finance_sync.enrichment.models import EnrichmentStatusSummary
from finance_sync.models.enrichment_freshness import EnrichmentFreshness
from finance_sync.models.security import Security

router = APIRouter(tags=["enrichment"])


@router.get("/enrichment/status")
async def get_enrichment_status(
    session=Depends(get_db),
) -> EnrichmentStatusSummary:
    """Return enrichment coverage and freshness statistics.

    Shows how many securities have been enriched, how many are
    pending, stale, or failed, and the last enrichment timestamp.
    """
    # Total securities
    total_result = await session.execute(
        select(func.count()).select_from(Security)
    )
    total_securities: int = total_result.scalar() or 0  # type: ignore[assignment]

    # Count by freshness status
    status_counts: dict[str, int] = {}
    status_query = await session.execute(
        select(
            EnrichmentFreshness.status,
            func.count(),
        ).group_by(EnrichmentFreshness.status)
    )
    for row in status_query:
        status_counts[str(row[0])] = int(row[1])

    # Securities with at least one price
    prices_result = await session.execute(
        select(func.count(func.distinct(EnrichmentFreshness.security_id)))  # type: ignore[attr-defined]
    )
    enriched: int = prices_result.scalar() or 0  # type: ignore[assignment]

    # Stale: enriched but not updated in the last 24h
    stale_cutoff = datetime.now(UTC) - timedelta(hours=24)
    stale_result = await session.execute(
        select(func.count())
        .select_from(EnrichmentFreshness)
        .where(
            EnrichmentFreshness.last_quote_fetch < stale_cutoff,  # type: ignore[attr-defined]
        )
    )
    stale: int = stale_result.scalar() or 0  # type: ignore[assignment]

    # Last enrichment run timestamp
    latest_result = await session.execute(
        select(func.max(EnrichmentFreshness.updated_at))
    )
    last_enrichment: datetime = latest_result.scalar()  # type: ignore[assignment]

    # Active data sources
    sources_result = await session.execute(
        select(func.distinct(EnrichmentFreshness.data_source))
    )
    data_sources: list[str] = [str(row[0]) for row in sources_result]

    return EnrichmentStatusSummary(
        total_securities=total_securities,
        enriched_securities=enriched,
        pending_securities=status_counts.get("pending", 0),
        failed_securities=status_counts.get("failed", 0),
        stale_securities=stale,
        last_enrichment_run=last_enrichment,
        data_sources=data_sources or ["openbb"],
    )
