"""Shared, typed helpers for read-service sorting and predicates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, desc

if TYPE_CHECKING:
    from collections.abc import Mapping


def sort_field(
    mapping: Mapping[str, Any],
    sort_by: str,
    sort_order: str = "desc",
) -> Any:
    """Return a safe ``order_by`` expression from an allowlisted mapping."""
    column = mapping.get(sort_by)
    if column is None:
        column = next(iter(mapping.values()))
        sort_order = "desc"
    return desc(column) if sort_order == "desc" else column.asc()


def expression(*conditions: Any) -> Any:
    """Combine optional SQL conditions without calling empty ``and_()``."""
    return and_(*conditions) if conditions else True
