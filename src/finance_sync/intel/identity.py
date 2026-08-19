"""Security-identity resolution for market-intelligence observations.

Observations carry candidate security identifiers (``ticker``/``isin``/
``figi``) extracted by the provider adapters.  This service resolves
them through the **existing** FIGI/ISIN/ticker/listing pipeline
(:class:`~finance_sync.enrichment.security_resolver.SecurityResolver`)
and applies the acceptance rule:

* **unambiguous match** — one canonical security, high confidence →
  the observation is linked to that security;
* **ambiguous match** — multiple identifiers resolve to *different*
  securities, or the single match has low/medium confidence → the
  observation is **never** silently linked; it is flagged
  ``review_required`` and a review-queue entry records the candidate
  list for a human (or a later, richer pass).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from finance_sync.intel.enums import IntelResolutionStatus

if TYPE_CHECKING:
    from finance_sync.enrichment.security_resolver import SecurityResolver
    from finance_sync.intel.models import IntelItem

#: Confidence values that are too weak to auto-link a holding.
#: ``ticker_only`` (a bare ticker without ISIN/FIGI confirmation) and
#: the fuzzy/inferred buckets always go to review.
LOW_CONFIDENCE = frozenset(
    {"medium", "low", "fuzzy", "inferred", "ticker_only", "ticker"}
)


class IntelIdentityResolutionError(Exception):
    """Raised when identity resolution cannot proceed safely."""


class IntelIdentityResolution:
    """Resolve observation identifiers to a canonical security.

    The result is a decision tuple consumed by the ingestion service:
    ``(security_id, status, review_required, candidates)`` where
    ``candidates`` is the list of matched securities (id + identifier +
    confidence) used to populate the review queue.
    """

    def __init__(self, resolver: SecurityResolver) -> None:
        self._resolver = resolver

    async def resolve(
        self,
        item: IntelItem,
    ) -> tuple[str | None, IntelResolutionStatus, bool, list[dict[str, Any]]]:
        """Resolve *item*'s identifiers.

        Returns ``(security_id, status, review_required, candidates)``.
        ``security_id`` is ``None`` whenever the match is ambiguous —
        the caller must never attach it to a holding.
        """
        identifiers = item.identifiers or {}
        candidates_list: list[tuple[str, str]] = []
        for id_type in ("isin", "figi", "ticker"):
            value = identifiers.get(id_type)
            if value:
                candidates_list.append((id_type, str(value)))
        if not candidates_list:
            return None, IntelResolutionStatus.UNRESOLVED, False, []

        matched: list[tuple[str, str, Any]] = []
        for id_type, value in candidates_list:
            result = await self._resolve_one(id_type, value)
            if result is not None:
                matched.append((id_type, value, result))

        if not matched:
            return None, IntelResolutionStatus.UNRESOLVED, False, []

        candidates = [
            {
                "identifier_type": id_type,
                "identifier": value,
                "security_id": str(result.security_id),
                "confidence": str(result.confidence or ""),
                "name": result.name,
                "ticker": result.ticker,
            }
            for id_type, value, result in matched
        ]

        distinct_ids = {str(result.security_id) for _, _, result in matched}
        if len(distinct_ids) > 1:
            # Multiple identifiers resolved to *different* securities —
            # never silently pick one.
            return (
                None,
                IntelResolutionStatus.AMBIGUOUS,
                True,
                candidates,
            )

        best = matched[0][2]
        confidence = (best.confidence or "").lower()
        if confidence in LOW_CONFIDENCE:
            # A single match, but too weak to auto-link: e.g. a bare
            # ticker that could name several instruments.  Review.
            return (
                None,
                IntelResolutionStatus.AMBIGUOUS,
                True,
                candidates,
            )

        return (
            str(best.security_id),
            IntelResolutionStatus.RESOLVED,
            False,
            candidates,
        )

    async def _resolve_one(self, id_type: str, value: str) -> Any | None:
        """Resolve one identifier via the existing pipeline.

        Returns a :class:`ResolvedSecurity`-like object or ``None``.
        The enrichment ``SecurityResolver`` returns
        ``UnresolvedSecurity`` DTOs for misses — those are treated as
        no-match here.
        """
        from finance_sync.enrichment.models import ResolvedSecurity

        if id_type == "isin":
            result = await self._resolver.resolve_by_isin(value)
        elif id_type == "figi":
            result = await self._resolver.resolve_by_figi(value)
        else:
            result = await self._resolver.resolve_by_ticker(value)
        if isinstance(result, ResolvedSecurity):
            return result
        return None
