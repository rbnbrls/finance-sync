"""Read service for the market-intelligence source layer.

Exposes tenant-scoped source metadata and structured facts — never
provider credentials and never full (unlicensed) article text.  The
licensing policy is enforced at ingestion time; this service only
*serves* what was stored, so a restricted item's ``body`` is always
``None`` here (the ingestion layer never persisted it).
"""

from __future__ import annotations

from datetime import (
    datetime,  # noqa: TC003 — needed by pydantic model_rebuild()
)
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import desc, func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.market_intelligence_provider_state import (
    MarketIntelligenceProviderState,
)
from finance_sync.models.market_intelligence_review_queue import (
    MarketIntelligenceReviewQueue,
)
from finance_sync.models.market_intelligence_run import (
    MarketIntelligenceRun,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class MarketIntelligenceItemDTO(BaseModel):
    """Tenant-scoped read projection of a stored observation.

    ``body`` is only populated for permissive license classes (the
    ingestion layer already dropped it otherwise); for every other class
    it is ``None`` and consumers get the canonical link instead.
    """

    id: str
    provider: str
    source_id: str
    canonical_url: str | None = None
    kind: str
    published_at: datetime
    fetched_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    language: str
    license_class: str
    license_uri: str | None = None
    content_hash: str
    headline: str | None = None
    summary: str | None = None
    body: str | None = None
    facts: list[dict[str, Any]] | None = None
    provider_metadata: dict[str, Any] | None = None
    identifiers: dict[str, str] | None = None
    resolution_status: str
    security_id: str | None = None
    review_required: bool
    stale_after: datetime | None = None
    is_stale: bool = False


class ProviderStateDTO(BaseModel):
    """Run/freshness state of one provider for a tenant."""

    provider: str
    status: str
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_class: str | None = None
    latency_ms: int | None = None
    items_ingested: int | None = None
    quota_used: int | None = None
    quota_limit: int | None = None
    freshness_max_age_seconds: int | None = None
    freshness_min_interval_seconds: int | None = None
    capabilities: list[str] | None = None
    availability: dict[str, str] | None = None


class IntelRunDTO(BaseModel):
    """One recorded scheduler run (run registry, tenant-scoped)."""

    id: str
    provider: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    latency_ms: int | None = None
    items_ingested: int | None = None
    quota_used: int | None = None
    quota_limit: int | None = None
    error: str | None = None
    error_class: str | None = None
    freshness_max_age_seconds: int | None = None
    freshness_min_interval_seconds: int | None = None
    capabilities: list[str] | None = None
    availability: dict[str, str] | None = None


class ReviewQueueDTO(BaseModel):
    """One review-queue entry (ambiguous security resolution)."""

    id: str
    item_id: str
    provider: str
    source_id: str
    candidate_identifiers: list[dict[str, Any]] | None = None
    resolution_status: str
    resolved_security_id: str | None = None
    review_note: str | None = None


class MarketIntelligenceListResponse(BaseModel):
    items: list[MarketIntelligenceItemDTO]
    total: int


class MarketIntelligenceReadService:
    """Tenant-scoped read queries for market-intelligence data."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Items ───────────────────────────────────────────────────────

    async def list_items(
        self,
        tenant_id: str,
        *,
        provider: str | None = None,
        kind: str | None = None,
        review_required: bool | None = None,
        is_stale: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MarketIntelligenceListResponse:
        """List stored observations for *tenant_id* (tenant-scoped)."""
        conditions: list[Any] = [
            MarketIntelligenceItem.tenant_id == tenant_id  # type: ignore[attr-defined]
        ]
        if provider:
            conditions.append(
                MarketIntelligenceItem.provider == provider  # type: ignore[attr-defined]
            )
        if kind:
            conditions.append(
                MarketIntelligenceItem.kind == kind  # type: ignore[attr-defined]
            )
        if review_required is not None:
            conditions.append(
                MarketIntelligenceItem.review_required.is_(review_required)  # type: ignore[attr-defined]
            )
        if is_stale is not None:
            conditions.append(
                MarketIntelligenceItem.is_stale.is_(is_stale)  # type: ignore[attr-defined]
            )

        count_stmt = (
            select(func.count())
            .select_from(MarketIntelligenceItem)
            .where(*conditions)
        )
        total: int = (await self._session.execute(count_stmt)).scalar() or 0  # type: ignore[assignment]

        stmt = (
            select(MarketIntelligenceItem)
            .where(*conditions)
            .order_by(desc(MarketIntelligenceItem.published_at))  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        rows = list(
            (await self._session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )

        return MarketIntelligenceListResponse(
            items=[_item_to_dto(row) for row in rows],
            total=int(total),
        )

    async def get_item(
        self,
        tenant_id: str,
        item_id: str,
    ) -> MarketIntelligenceItemDTO | None:
        """Return one observation iff it belongs to *tenant_id*.

        Returns ``None`` for a cross-tenant id — the caller maps that
        to 403/404, so tenant B can never read tenant A's record.
        """
        row = await self._session.get(MarketIntelligenceItem, item_id)
        if row is None or str(row.tenant_id) != str(tenant_id):
            return None
        return _item_to_dto(row)

    # ── Provider state ──────────────────────────────────────────────

    async def list_provider_states(
        self,
        tenant_id: str,
    ) -> list[ProviderStateDTO]:
        """Return run/freshness state of every provider for *tenant_id*."""
        stmt = (
            select(MarketIntelligenceProviderState)
            .where(
                MarketIntelligenceProviderState.tenant_id == tenant_id  # type: ignore[attr-defined]
            )
            .order_by(MarketIntelligenceProviderState.provider.asc())  # type: ignore[attr-defined]
        )
        rows = list(
            (await self._session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        return [_state_to_dto(row) for row in rows]

    # ── Run registry ───────────────────────────────────────────────

    async def list_runs(
        self,
        tenant_id: str,
        *,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IntelRunDTO]:
        """Return the recorded scheduler runs for *tenant_id*.

        The run registry is the observable history behind the
        latest-run state table: every run (started/completed, duration,
        quota, freshness, sanitised errors) is queryable, newest first.
        """
        conditions: list[Any] = [
            MarketIntelligenceRun.tenant_id == tenant_id  # type: ignore[attr-defined]
        ]
        if provider:
            conditions.append(
                MarketIntelligenceRun.provider == provider  # type: ignore[attr-defined]
            )
        if status:
            conditions.append(
                MarketIntelligenceRun.status == status  # type: ignore[attr-defined]
            )
        stmt = (
            select(MarketIntelligenceRun)
            .where(*conditions)
            .order_by(desc(MarketIntelligenceRun.started_at))  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        rows = list(
            (await self._session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        return [_run_to_dto(row) for row in rows]

    # ── Review queue ────────────────────────────────────────────────

    async def list_review_queue(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReviewQueueDTO]:
        """Return review-queue entries for *tenant_id*."""
        conditions: list[Any] = [
            MarketIntelligenceReviewQueue.tenant_id == tenant_id  # type: ignore[attr-defined]
        ]
        if status:
            conditions.append(
                MarketIntelligenceReviewQueue.resolution_status == status  # type: ignore[attr-defined]
            )
        stmt = (
            select(MarketIntelligenceReviewQueue)
            .where(*conditions)
            .order_by(
                desc(MarketIntelligenceReviewQueue.created_at)  # type: ignore[attr-defined]
            )
            .offset(offset)
            .limit(limit)
        )
        rows = list(
            (await self._session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        return [review_to_dto(row) for row in rows]


def _item_to_dto(row: MarketIntelligenceItem) -> MarketIntelligenceItemDTO:
    """Project an ORM row to the read DTO."""
    return MarketIntelligenceItemDTO(
        id=str(row.id),
        provider=row.provider,
        source_id=row.source_id,
        canonical_url=row.canonical_url,
        kind=row.kind,
        published_at=row.published_at,
        fetched_at=row.fetched_at,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        language=row.language,
        license_class=row.license_class,
        license_uri=row.license_uri,
        content_hash=row.content_hash,
        headline=row.headline,
        summary=row.summary,
        body=row.body,
        facts=row.facts,
        provider_metadata=row.provider_metadata,
        identifiers=row.identifiers,
        resolution_status=row.resolution_status,
        security_id=str(row.security_id) if row.security_id else None,
        review_required=row.review_required,
        stale_after=row.stale_after,
        is_stale=row.is_stale,
    )


def _state_to_dto(row: MarketIntelligenceProviderState) -> ProviderStateDTO:
    """Project a provider-state row to the read DTO."""
    return ProviderStateDTO(
        provider=row.provider,
        status=row.status,
        last_run_at=row.last_run_at,
        last_success_at=row.last_success_at,
        last_error=row.last_error,
        last_error_class=row.last_error_class,
        latency_ms=row.latency_ms,
        items_ingested=row.items_ingested,
        quota_used=row.quota_used,
        quota_limit=row.quota_limit,
        freshness_max_age_seconds=row.freshness_max_age_seconds,
        freshness_min_interval_seconds=row.freshness_min_interval_seconds,
        capabilities=row.capabilities,
        availability=row.availability,
    )


def _run_to_dto(row: MarketIntelligenceRun) -> IntelRunDTO:
    """Project a run-registry row to the read DTO."""
    return IntelRunDTO(
        id=str(row.id),
        provider=row.provider,
        started_at=row.started_at,
        completed_at=row.completed_at,
        status=row.status,
        latency_ms=row.latency_ms,
        items_ingested=row.items_ingested,
        quota_used=row.quota_used,
        quota_limit=row.quota_limit,
        error=row.error,
        error_class=row.error_class,
        freshness_max_age_seconds=row.freshness_max_age_seconds,
        freshness_min_interval_seconds=row.freshness_min_interval_seconds,
        capabilities=row.capabilities,
        availability=row.availability,
    )


def review_to_dto(row: MarketIntelligenceReviewQueue) -> ReviewQueueDTO:
    """Project a review-queue row to the read DTO."""
    return ReviewQueueDTO(
        id=str(row.id),
        item_id=str(row.item_id),
        provider=row.provider,
        source_id=row.source_id,
        candidate_identifiers=row.candidate_identifiers,
        resolution_status=row.resolution_status,
        resolved_security_id=str(row.resolved_security_id)
        if row.resolved_security_id
        else None,
        review_note=row.review_note,
    )
