"""Deterministic SQL statement counting for read performance tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class QueryCounter:
    """Count statements emitted by one SQLAlchemy engine.

    The counter observes the synchronous engine behind an ``AsyncEngine`` and
    is deliberately independent of the database dialect.  It counts SQL
    statements, not rows or network round trips, which makes query-budget
    regressions deterministic in CI.
    """

    def __init__(self, engine: Engine | AsyncEngine) -> None:
        self._engine = (
            engine.sync_engine if isinstance(engine, AsyncEngine) else engine
        )
        self.queries = 0
        self._listener = self._on_before_cursor_execute

    def _on_before_cursor_execute(self, *_args: Any, **_kwargs: Any) -> None:
        self.queries += 1

    def __enter__(self) -> QueryCounter:
        event.listen(self._engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *_args: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._listener)
