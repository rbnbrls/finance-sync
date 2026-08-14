"""Shared aggregate-response metadata — as-of / freshness / coverage.

Every aggregate endpoint (allocation, cashflow, performance,
subscriptions) declares this envelope so clients can judge how current
and how complete the underlying data is.  This fulfils the
``meta: {asOf, currency, nextCursor, freshness}`` envelope promised by
``docs/API.md``.

Freshness horizon
-----------------
Aggregates derive their ``freshness`` value from the age of the data
they are built on (``as_of``).  A fixed 24-hour staleness horizon is
used, matching the ``/enrichment/status`` stale-securities definition
and the default price-cache TTL — no settings dependency is needed in
the read services.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_PARTIAL = "partial"
FRESHNESS_UNKNOWN = "unknown"

#: Horizon after which aggregate data is considered stale (24h, matching
#: the ``/enrichment/status`` stale_securities definition).
AGGREGATE_STALE_AFTER: timedelta = timedelta(hours=24)


def freshness_for(
    as_of: datetime | None,
    *,
    now: datetime | None = None,
    stale_after: timedelta = AGGREGATE_STALE_AFTER,
) -> str:
    """Classify the freshness of an aggregate given its ``as_of`` time.

    Returns one of ``fresh`` / ``stale`` / ``unknown`` (``partial`` is
    reserved for callers that have per-item freshness data and want to
    signal a mix of fresh and stale inputs).
    """
    if as_of is None:
        return FRESHNESS_UNKNOWN
    reference = now or datetime.now(UTC)
    if reference - as_of > stale_after:
        return FRESHNESS_STALE
    return FRESHNESS_FRESH


class CoverageInfo(BaseModel):
    """Counts describing how complete an aggregate computation was."""

    accounts: int = Field(
        default=0,
        description="Number of accounts included in the computation",
    )
    holdings: int = Field(
        default=0,
        description="Number of holdings/positions included",
    )
    priced_holdings: int = Field(
        default=0,
        description="Holdings with a market value / price",
    )
    stale_holdings: int = Field(
        default=0,
        description=(
            "Holdings whose underlying observation is older than the "
            "staleness horizon"
        ),
    )
    items: int = Field(
        default=0,
        description=(
            "Rows considered by the computation (e.g. transactions, "
            "subscriptions)"
        ),
    )


class CollectionMeta(BaseModel):
    """Pagination/freshness envelope for collection endpoints.

    Fulfils the ``meta: {asOf, currency, nextCursor, freshness}``
    envelope promised by ``docs/API.md`` for collection endpoints.
    Fields may be null where not applicable (offset pagination has no
    opaque cursor; mixed-currency collections have no single currency).
    """

    as_of: datetime | None = Field(
        default=None,
        description=(
            "Timestamp of the underlying data (latest holding/"
            "transaction/price observation); null when no data exists"
        ),
    )
    currency: str | None = Field(
        default=None,
        description=(
            "ISO-4217 currency when the collection is single-currency; "
            "null when mixed or not applicable"
        ),
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque pagination cursor for the next page; null with "
            "offset-based pagination"
        ),
    )
    freshness: str = Field(
        default=FRESHNESS_UNKNOWN,
        description="Data currency: fresh | stale | partial | unknown",
    )


class AggregateMeta(BaseModel):
    """As-of / freshness / coverage envelope for aggregate responses."""

    as_of: datetime | None = Field(
        default=None,
        description="Timestamp the underlying data was observed",
    )
    freshness: str = Field(
        default=FRESHNESS_UNKNOWN,
        description="Data currency: fresh | stale | partial | unknown",
    )
    coverage: CoverageInfo | None = Field(
        default=None,
        description="Coverage counts for the aggregate computation",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Human-readable caveats about data quality",
    )


def build_meta(
    *,
    as_of: datetime | None = None,
    now: datetime | None = None,
    coverage: CoverageInfo | None = None,
    caveats: list[str] | None = None,
    freshness: str | None = None,
) -> AggregateMeta:
    """Build an :class:`AggregateMeta` envelope with derived freshness.

    When ``freshness`` is omitted it is derived from ``as_of`` via
    :func:`freshness_for`.  ``as_of`` is kept ``None`` when no data
    was available (callers that want an explicit "computed now" stamp
    pass ``as_of=datetime.now(UTC)`` themselves).
    """
    reference = now or datetime.now(UTC)
    return AggregateMeta(
        as_of=as_of,
        freshness=freshness or freshness_for(as_of, now=reference),
        coverage=coverage,
        caveats=caveats or [],
    )


__all__ = [
    "AGGREGATE_STALE_AFTER",
    "FRESHNESS_FRESH",
    "FRESHNESS_PARTIAL",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "AggregateMeta",
    "CollectionMeta",
    "CoverageInfo",
    "build_meta",
    "freshness_for",
]
