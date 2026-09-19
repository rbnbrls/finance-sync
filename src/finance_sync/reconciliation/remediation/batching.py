"""Strategy-neutral compatibility and small request coalescing helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterable


def compatibility_key(item: Any) -> str:
    context = _context(item)
    parts = (
        item.provider_key,
        str(item.connection_id or "-"),
        item.remediation_strategy,
        str(context.get("endpoint_family", "default")),
        str(context.get("auth_scope", "-")),
        str(context.get("currency", "-")),
        str(context.get("interval", "-")),
        str(context.get("account_id", "-")),
        str(context.get("provider_account_id", "-")),
        str(context.get("request_shape", "default")),
        str(context.get("security_id", "-")),
        str(context.get("identifier", "-")),
        str(context.get("identifier_type", "-")),
        str(context.get("limit", "-")),
    )
    return "|".join(parts)


def _context(item: Any) -> dict[str, Any]:
    value = getattr(item, "context", {})
    return (
        dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}
    )


def group_compatible(items: Iterable[Any]) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        groups[compatibility_key(item)].append(item)
    return dict(groups)


@dataclass(frozen=True, slots=True)
class RemediationBatch:
    compatibility: str
    items: tuple[Any, ...]


def create_batches(
    items: Iterable[Any], *, batch_limit: int = 50
) -> list[RemediationBatch]:
    """Group compatible work and split it at the provider batch limit."""
    if batch_limit < 1:
        message = "batch_limit must be positive"
        raise ValueError(message)
    batches: list[RemediationBatch] = []
    for key, group in group_compatible(items).items():
        for start in range(0, len(group), batch_limit):
            batches.extend(
                [
                    RemediationBatch(
                        key, tuple(group[start : start + batch_limit])
                    )
                ]
            )
    return batches


def coalesce_days(
    days: Iterable[date], *, max_window_days: int = 31
) -> list[tuple[date, date]]:
    """Merge adjacent days, splitting ranges at the configured maximum."""
    ordered = sorted(set(days))
    if not ordered:
        return []
    ranges: list[tuple[date, date]] = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        if (
            current != previous + timedelta(days=1)
            or (current - start).days + 1 > max_window_days
        ):
            ranges.append((start, previous))
            start = current
        previous = current
    ranges.append((start, previous))
    return ranges
