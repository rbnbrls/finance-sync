"""Manual resolution of market-intelligence review-queue entries.

When an observation's security identity is ambiguous (multiple
distinct candidate securities, or a single low-confidence match) the
item is stored without a holding link and one review-queue entry is
created.  A human operator can resolve the entry here: choosing one
candidate security links the observation to that security and marks
the queue entry ``resolved``.

The operation is tenant-scoped, idempotent and never re-opens an
already-resolved entry without an explicit call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from finance_sync.intel.enums import IntelResolutionStatus
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.market_intelligence_review_queue import (
    INTEL_REVIEW_STATUSES,
    MarketIntelligenceReviewQueue,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ReviewQueueError(ValueError):
    """Raised on an invalid manual-resolution operation."""


class IntelReviewService:
    """Resolves review-queue entries for ambiguous market-intelligence items."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_entry(
        self,
        tenant_id: str,
        entry_id: str,
        security_id: str,
        *,
        note: str | None = None,
        resolver_principal: str = "operator",
    ) -> MarketIntelligenceReviewQueue | None:
        """Resolve *entry_id* to *security_id* for *tenant_id*.

        Updates the queue entry to ``resolved`` and links the
        underlying observation to the chosen security.  Returns the
        updated entry, or ``None`` when the entry does not belong to
        the tenant or does not exist.

        Raises :class:`ReviewQueueError` when the target security does
        not exist.
        """
        entry = await self._get_entry(tenant_id, entry_id)
        if entry is None:
            return None

        # Verify the target security exists.
        from finance_sync.models.security import Security

        target = await self._session.get(Security, security_id)
        if target is None:
            msg = f"target security {security_id!r} does not exist"
            raise ReviewQueueError(msg)

        # Link the observation.
        item = await self._session.get(MarketIntelligenceItem, entry.item_id)
        if item is not None:
            item.security_id = security_id
            item.resolution_status = IntelResolutionStatus.RESOLVED.value
            item.review_required = False
            item.updated_at = datetime.now(UTC)

        # Mark the queue entry resolved.
        entry.resolution_status = "resolved"
        entry.resolved_security_id = security_id
        if note:
            entry.review_note = (
                f"{note} (resolved by {resolver_principal} at "
                f"{datetime.now(UTC).isoformat()})"
            )
        else:
            entry.review_note = (
                f"Resolved by {resolver_principal} at "
                f"{datetime.now(UTC).isoformat()}"
            )
        entry.updated_at = datetime.now(UTC)
        await self._session.flush()
        return entry

    async def dismiss_entry(
        self,
        tenant_id: str,
        entry_id: str,
        *,
        note: str | None = None,
        resolver_principal: str = "operator",
    ) -> MarketIntelligenceReviewQueue | None:
        """Dismiss *entry_id* (no security is chosen).

        Marks the queue entry ``dismissed``; the observation keeps no
        security link and is no longer flagged for review.  Returns the
        updated entry or ``None`` when the entry is not the tenant's.
        """
        entry = await self._get_entry(tenant_id, entry_id)
        if entry is None:
            return None
        entry.resolution_status = "dismissed"
        entry.resolved_security_id = None
        entry.review_note = (
            f"{note or 'Dismissed'} (by {resolver_principal} at "
            f"{datetime.now(UTC).isoformat()})"
        )
        entry.updated_at = datetime.now(UTC)

        # Clear the review flag on the observation (keep it unlinked).
        item = await self._session.get(MarketIntelligenceItem, entry.item_id)
        if item is not None:
            item.review_required = False
            item.updated_at = datetime.now(UTC)
        await self._session.flush()
        return entry

    async def _get_entry(
        self,
        tenant_id: str,
        entry_id: str,
    ) -> MarketIntelligenceReviewQueue | None:
        """Return one review-queue entry scoped to *tenant_id*."""
        from sqlalchemy import select

        stmt = select(MarketIntelligenceReviewQueue).where(
            MarketIntelligenceReviewQueue.id == entry_id,
            MarketIntelligenceReviewQueue.tenant_id == tenant_id,
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()


def validate_review_status(status: str) -> None:
    """Raise :class:`ReviewQueueError` when *status* is not a valid one."""
    if status not in INTEL_REVIEW_STATUSES:
        msg = (
            f"invalid review-queue status {status!r}; expected one of "
            f"{sorted(INTEL_REVIEW_STATUSES)}"
        )
        raise ReviewQueueError(msg)
