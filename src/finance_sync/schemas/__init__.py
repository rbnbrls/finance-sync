"""API schemas for request/response serialization.

Schemas mirror the ORM models but are Pydantic-only, with no
database dependency. They support both serialization (model_dump)
and deserialization (model_validate) for API endpoints.
"""

from __future__ import annotations

from finance_sync.schemas.freshness import (
    AGGREGATE_STALE_AFTER,
    FRESHNESS_FRESH,
    FRESHNESS_PARTIAL,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    AggregateMeta,
    CoverageInfo,
    build_meta,
    freshness_for,
)
from finance_sync.schemas.fx_rate import FxRateCreate, FxRateResponse

__all__ = [
    "AGGREGATE_STALE_AFTER",
    "FRESHNESS_FRESH",
    "FRESHNESS_PARTIAL",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "AggregateMeta",
    "CoverageInfo",
    "FxRateCreate",
    "FxRateResponse",
    "build_meta",
    "freshness_for",
]
