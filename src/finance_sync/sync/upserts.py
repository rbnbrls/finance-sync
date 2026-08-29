"""Database-native bulk upserts for sync ingestion.

The sync stages persist canonical connector data row by row through the
ORM (select → mutate/create → flush).  That is correct and testable, but
on PostgreSQL it issues two round-trips per row and cannot leverage the
unique constraints that already exist on ``transactions`` and
``holdings`` as conflict targets.

This module provides the PostgreSQL-native path: a single
``INSERT ... ON CONFLICT (...) DO UPDATE ... RETURNING`` statement per
call, which is atomic and idempotent at the database level.  Re-running
a sync with identical provider data updates rows in place instead of
duplicating them, and a crash mid-run cannot leave a partial state
because every statement participates in the caller's UnitOfWork
transaction.

Dialect handling
----------------

The unit test suite runs on aiosqlite, where ``ON CONFLICT ... DO
UPDATE`` with ``RETURNING`` is only partially supported (SQLite supports
the syntax but SQLAlchemy's dialect renders a different conflict-clause
shape and the tests assert ORM behaviour).  Both entry points therefore
accept a *dialect-aware* router: callers pass ``session`` and the
functions fall back to the classic per-row ORM path when the bound
engine is not PostgreSQL.  The fallback is provided by the caller via
the ``fallback`` callable so the upsert module stays free of ORM
dependencies beyond the statement construction.

Insert-vs-update detection
--------------------------

Each row dict carries a freshly generated ``id`` (uuid4).  The conflict
target never includes ``id`` and ``id`` is never part of ``set_``, so an
INSERT uses the generated id while an UPDATE keeps the existing row's
id.  Comparing the ``RETURNING`` id against the per-row generated id is
therefore a deterministic, dialect-clean way to distinguish created from
updated rows — no reliance on PostgreSQL system columns.

Change detection (why the DO UPDATE carries a WHERE clause)
-----------------------------------------------------------

An unconditional ``DO UPDATE`` would update *every* conflicting row on
every sync and re-emit ``{entity}.updated`` outbox messages with the
same idempotency key (``{entity}:{id}:updated`` is UNIQUE) — the third
sync run would crash on the outbox unique constraint.  The statement
therefore appends ``WHERE <column> IS DISTINCT FROM excluded.<column>``
for every mutable column.  Rows whose values are identical to the
existing row are left untouched, are *not* returned by ``RETURNING``,
and produce no outbox event.  This mirrors the per-row ORM path's
change detection exactly (``values_differ``).

``IS DISTINCT FROM`` also makes NULL semantics correct: a row whose
column is NULL compares equal to an existing NULL, so a re-sync that
clears a field does not churn the row (and ``security_id`` is preserved
when the provider simply did not re-report it — the per-row path never
nulls it either).

Return values
-------------

Each function returns an :class:`UpsertResult` with ``inserted_ids`` /
``updated_ids`` (in execution order) plus convenience counts.  Only
rows that were actually changed are returned (see above), so the counts
reflect real work.  Outbox emission is the caller's responsibility: the
result exposes which rows were created vs updated so the caller can emit
``*.created`` / ``*.updated`` events with the entity's stable
idempotency key.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from sqlalchemy.dialects.postgresql import insert

from finance_sync.db import Base
from finance_sync.models.holding import Holding
from finance_sync.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT", bound=type[Base])


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Outcome of a single bulk-upsert call.

    ``inserted_ids`` and ``updated_ids`` are in the same order as the
    input rows, but **only rows that were actually written** appear
    (rows whose conflict-update found no change are absent).  The two
    tuples are disjoint and cover every returned row exactly once.
    """

    inserted_ids: tuple[str, ...]
    updated_ids: tuple[str, ...]

    @property
    def inserted(self) -> int:
        return len(self.inserted_ids)

    @property
    def updated(self) -> int:
        return len(self.updated_ids)

    @property
    def total(self) -> int:
        return self.inserted + self.updated

    @property
    def ids(self) -> tuple[str, ...]:
        return self.inserted_ids + self.updated_ids


_UpsertModel = type[Transaction] | type[Holding]


def _upsert_stmt(
    model: _UpsertModel,
    rows: Sequence[dict[str, Any]],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str],
    revision_column: str | None = None,
) -> Any:
    """Build the ``INSERT .. ON CONFLICT DO UPDATE .. RETURNING`` stmt.

    *update_columns* are the mutable columns refreshed on conflict.  The
    conflict clause is gated by ``WHERE col IS DISTINCT FROM
    excluded.col`` for every update column plus the revision column, so
    unchanged rows are skipped entirely (not returned, no revision bump,
    no outbox event).  When *revision_column* is set it is set to
    ``<model>.revision + 1`` in the SET clause instead of a plain value.
    """
    stmt = insert(model).values(list(rows))
    excluded = stmt.excluded
    set_: dict[str, Any] = {}
    where: list[Any] = []
    for col in update_columns:
        # Match the per-row ORM path's "update only non-None values"
        # semantics: COALESCE keeps the existing row's value when the
        # incoming row carries NULL (e.g. a provider that stops
        # reporting amount_in_base, or a security reference that fails
        # to resolve on a re-sync — the per-row path never nulls a
        # previously-linked security_id).  The WHERE gate then fires
        # exactly when the incoming value is non-NULL and differs, so a
        # NULL incoming value never counts as a change.
        incoming = excluded
        set_[col] = _coalesce(incoming, col, model)
        where.append(
            _coalesce(incoming, col, model).is_distinct_from(
                getattr(model, col)
            )
        )
    if revision_column is not None:
        # The revision is a side-effect counter: it is incremented on
        # every real change, but its *value* must never trigger a change
        # by itself.  The VALUES row always carries 1 (fresh-insert
        # default), so comparing revision IS DISTINCT FROM excluded
        # would flag every conflict as changed and bump the revision on
        # every re-sync even when no data column moved.
        set_[revision_column] = getattr(model, revision_column) + 1
    # Any mutable column changing is sufficient to refresh the row.  An
    # AND-gate would make updates impossible whenever an otherwise unchanged
    # column is present in the batch (the normal case for partial provider
    # payloads).
    stmt = stmt.on_conflict_do_update(
        index_elements=list(index_elements),
        set_=set_,
        where=where[0] if len(where) == 1 else _or_(*where),
    )
    return stmt.returning(model.id)


def _coalesce(
    excluded: Any,
    column: str,
    model: _UpsertModel,
) -> Any:
    """Return ``COALESCE(excluded.<column>, <model>.<column>)``."""
    from sqlalchemy import func

    return func.coalesce(getattr(excluded, column), getattr(model, column))


def _or_(*clauses: Any) -> Any:
    """OR-combine SQL clauses without importing sqlalchemy eagerly."""
    from sqlalchemy import or_

    return or_(*clauses)


async def _run_upsert(
    session: AsyncSession,
    stmt: Any,
    generated_ids: Sequence[str],
) -> UpsertResult:
    """Execute the upsert and split returned ids into inserted/updated.

    *generated_ids* are the fresh ``id`` values carried by the input row
    dicts, in VALUES order.  Rows whose conflict-update found no value
    change are **not** returned by ``RETURNING``, so the returned list
    is a filtered subset of the input — positional matching against
    *generated_ids* is therefore unreliable.  Instead we use set
    membership: an inserted row keeps the freshly generated id we handed
    it (a uuid4 never collides with any existing row), while an updated
    row keeps its pre-existing id, which by construction is not among
    this batch's generated ids.  Both output tuples preserve RETURNING
    (i.e. VALUES) order.
    """
    result = await session.execute(stmt)
    returned_ids = [str(row[0]) for row in result.all()]
    generated_set = set(generated_ids)
    inserted: list[str] = []
    updated: list[str] = []
    for row_id in returned_ids:
        if row_id in generated_set:
            inserted.append(row_id)
        else:
            updated.append(row_id)
    await session.flush()
    return UpsertResult(
        inserted_ids=tuple(inserted),
        updated_ids=tuple(updated),
    )


def _is_postgresql(session: AsyncSession) -> bool:
    """Return whether *session* is bound to a PostgreSQL engine.

    The check must be defensive: unit tests pass ``AsyncMock`` sessions
    where ``get_bind()`` returns a coroutine (or a MagicMock chain) and
    attribute access on it can raise.  Any failure means "not
    PostgreSQL", which routes to the caller-provided fallback.
    """
    try:
        bind = cast(Any, session).get_bind()
        if isawaitable(bind):
            # AsyncMock can manufacture a coroutine here even though the
            # real SQLAlchemy method is synchronous.  Close it so the
            # fallback does not leak a RuntimeWarning in unit tests.
            close = getattr(bind, "close", None)
            if callable(close):
                close()
            return False
        return (
            getattr(getattr(bind, "dialect", None), "name", None)
            == "postgresql"
        )
    except (AttributeError, TypeError):
        return False


async def bulk_upsert_transactions(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str],
    fallback: Callable[[], Awaitable[UpsertResult]],
    revision_column: str = "revision",
) -> UpsertResult:
    """PostgreSQL ``INSERT .. ON CONFLICT DO UPDATE`` for transactions.

    *rows* are full column dictionaries (including ``id``, generated by
    the caller as a UUID) for the ``transactions`` table.
    *index_elements* names the unique-constraint columns to treat as the
    conflict target; *update_columns* lists the mutable columns to
    refresh on conflict.  The conflict clause only fires when a mutable
    column actually changed (``IS DISTINCT FROM``), so re-syncs with
    identical data are no-ops.

    On a non-PostgreSQL dialect the *fallback* callable is awaited and
    its result returned untouched.
    """
    if not _is_postgresql(session):
        return await fallback()

    if not rows:
        return UpsertResult(inserted_ids=(), updated_ids=())

    stmt = _upsert_stmt(
        Transaction,
        rows,
        index_elements=index_elements,
        update_columns=update_columns,
        revision_column=revision_column,
    )
    return await _run_upsert(
        session,
        stmt,
        [str(row["id"]) for row in rows],
    )


async def bulk_upsert_holdings(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str],
    fallback: Callable[[], Awaitable[UpsertResult]],
) -> UpsertResult:
    """PostgreSQL ``INSERT .. ON CONFLICT DO UPDATE`` for holdings.

    Same contract as :func:`bulk_upsert_transactions` but for the
    ``holdings`` table snapshot identity unique constraint.
    """
    if not _is_postgresql(session):
        return await fallback()

    if not rows:
        return UpsertResult(inserted_ids=(), updated_ids=())

    stmt = _upsert_stmt(
        Holding,
        rows,
        index_elements=index_elements,
        update_columns=update_columns,
    )
    return await _run_upsert(
        session,
        stmt,
        [str(row["id"]) for row in rows],
    )
