"""Strict async-session doubles for persistence and reconciliation tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy.ext.asyncio import AsyncSession


def make_async_session() -> MagicMock:
    """Return a session-shaped double with sync and async methods aligned."""
    # MagicMock derives async methods such as execute/scalars/commit from the
    # AsyncSession spec while keeping sync ORM methods such as add as mocks.
    return MagicMock(spec=AsyncSession)
