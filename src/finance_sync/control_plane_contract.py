"""Shared control-plane vocabulary and deterministic projection rules.

This module intentionally contains no database or HTTP dependencies.  It is
the single place for the phase-0 contract decisions that are reused by the
aggregation service and its tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from finance_sync.schemas.control_plane import (
        FreshnessStatus,
        OverviewStatus,
    )

SyncStatus = Literal[
    "running",
    "completed",
    "failed",
    "partial",
    "skipped",
    "cancelled",
]

ExportStatus = Literal["running", "completed", "failed", "cancelled"]

CONTROL_PLANE_ERROR_CATEGORIES = frozenset(
    {
        "authentication",
        "provider_unavailable",
        "rate_limited",
        "validation",
        "data_mapping",
        "database",
        "unknown",
    }
)


def overview_status(
    *,
    failed_syncs: int,
    issues_open: int,
    failed_destinations: int,
    freshness_status: FreshnessStatus,
) -> OverviewStatus:
    """Apply the documented precedence rules to an overview snapshot.

    A failed sync is the most urgent operational state.  Other actionable
    problems require attention, while incomplete freshness without another
    issue is reported as partial availability.
    """

    if failed_syncs > 0:
        return "sync_failed"
    if issues_open > 0 or failed_destinations > 0:
        return "attention_required"
    if freshness_status in {"partial", "unavailable"}:
        return "partial"
    return "healthy"


def latest_timestamp(
    values: Iterable[datetime | None],
) -> datetime | None:
    """Return the newest source timestamp, or ``None`` when there is no data."""

    return max((value for value in values if value is not None), default=None)
