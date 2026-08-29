"""Tests for the database-native bulk upsert paths.

Covers three layers:

1. :mod:`finance_sync.sync.upserts` — the PostgreSQL ``ON CONFLICT DO
   UPDATE`` statement builders and the insert-vs-update detection,
   against both real SQLite (dialect fallback) and a mocked PostgreSQL
   session (statement shape + result splitting).
2. ``TransactionPersistence.persist_transactions_batch`` /
   ``HoldingPersistence.persist_holdings_batch`` — the row-building and
   outbox emission for the batch path, including the per-row fallback
   behaviour on non-PostgreSQL sessions.
3. The sync stages' batch dispatch (batch writer used when available,
   per-row fallback otherwise).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.connectors.models import (
    CanonicalHoldingData,
    CanonicalTransactionData,
    SecurityReference,
)
from finance_sync.sync.upserts import (
    UpsertResult,
    _is_postgresql,
    _upsert_stmt,
    bulk_upsert_holdings,
    bulk_upsert_transactions,
)


def _fake_pg_session(*, rows: list[tuple] | None = None) -> MagicMock:
    """Return a session whose bind reports the postgresql dialect."""
    session = MagicMock()
    bind = MagicMock()
    dialect = MagicMock()
    dialect.name = "postgresql"
    bind.dialect = dialect
    session.get_bind.return_value = bind
    result = MagicMock()
    result.all.return_value = rows or []
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    return session


def _fake_sqlite_session() -> MagicMock:
    session = MagicMock()
    bind = MagicMock()
    dialect = MagicMock()
    dialect.name = "sqlite"
    bind.dialect = dialect
    session.get_bind.return_value = bind
    return session


class TestIsPostgresql:
    def test_postgresql_dialect(self) -> None:
        assert _is_postgresql(_fake_pg_session()) is True

    def test_sqlite_dialect(self) -> None:
        assert _is_postgresql(_fake_sqlite_session()) is False

    def test_mock_session_falls_back_to_false(self) -> None:
        """AsyncMock sessions (unit tests) must never be treated as PG."""
        session = AsyncMock()
        assert _is_postgresql(session) is False

    def test_get_bind_raises_falls_back_to_false(self) -> None:
        session = MagicMock()
        session.get_bind.side_effect = AttributeError("no bind")
        assert _is_postgresql(session) is False


class TestUpsertStatement:
    def test_transaction_stmt_uses_conflict_target(self) -> None:
        from finance_sync.models.transaction import Transaction

        stmt = _upsert_stmt(
            Transaction,
            [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "tenant_id": "tenant-1",
                    "provider_key": "trading212",
                    "connection_id": None,
                    "external_transaction_id": "tx-1",
                    "account_id": "account-1",
                    "amount": Decimal("1.00"),
                    "currency_code": "EUR",
                    "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "transaction_type": "deposit",
                }
            ],
            index_elements=(
                "tenant_id",
                "provider_key",
                "connection_id",
                "external_transaction_id",
            ),
            update_columns=("amount", "currency_code", "occurred_at"),
        )
        compiled = str(
            stmt.compile(
                dialect=__import__("sqlalchemy").dialects.postgresql.dialect()
            )
        )
        assert "ON CONFLICT" in compiled
        assert "tenant_id" in compiled
        assert "external_transaction_id" in compiled
        assert "DO UPDATE" in compiled
        assert "RETURNING" in compiled

    def test_holding_stmt_uses_snapshot_identity(self) -> None:
        from finance_sync.models.holding import Holding

        stmt = _upsert_stmt(
            Holding,
            [
                {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "tenant_id": "tenant-1",
                    "account_id": "account-1",
                    "security_id": "security-1",
                    "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "source": "provider_sync",
                    "quantity": Decimal(2),
                    "currency_code": "EUR",
                }
            ],
            index_elements=(
                "tenant_id",
                "account_id",
                "security_id",
                "observed_at",
                "source",
            ),
            update_columns=("quantity", "market_value"),
        )
        compiled = str(
            stmt.compile(
                dialect=__import__("sqlalchemy").dialects.postgresql.dialect()
            )
        )
        assert "ON CONFLICT" in compiled
        assert "observed_at" in compiled
        assert "DO UPDATE" in compiled


class TestBulkUpsertDispatch:
    async def test_transactions_fallback_on_sqlite(self) -> None:
        session = _fake_sqlite_session()
        fallback = AsyncMock(
            return_value=UpsertResult(
                inserted_ids=("a", "b"),
                updated_ids=(),
            )
        )
        result = await bulk_upsert_transactions(
            session,
            [{"id": "a"}, {"id": "b"}],
            index_elements=("tenant_id",),
            update_columns=("amount",),
            fallback=fallback,
        )
        assert result.inserted == 2
        assert result.updated == 0
        fallback.assert_awaited_once()

    async def test_transactions_empty_rows_short_circuits(self) -> None:
        session = _fake_pg_session()
        fallback = AsyncMock(return_value=UpsertResult((), ()))
        result = await bulk_upsert_transactions(
            session,
            [],
            index_elements=("tenant_id",),
            update_columns=("amount",),
            fallback=fallback,
        )
        assert result.total == 0
        fallback.assert_not_awaited()
        session.execute.assert_not_awaited()

    async def test_holdings_fallback_on_sqlite(self) -> None:
        session = _fake_sqlite_session()
        fallback = AsyncMock(
            return_value=UpsertResult(
                inserted_ids=("h1",),
                updated_ids=("h0",),
            )
        )
        result = await bulk_upsert_holdings(
            session,
            [{"id": "h0"}, {"id": "h1"}],
            index_elements=("tenant_id",),
            update_columns=("quantity",),
            fallback=fallback,
        )
        assert result.inserted == 1
        assert result.updated == 1
        fallback.assert_awaited_once()

    async def test_pg_insert_update_detection(self) -> None:
        """Returned ids equal to generated ids are inserts; others updates."""
        from uuid import uuid4

        fresh_id = str(uuid4())
        existing_id = "99999999-9999-4999-8999-999999999999"
        # VALUES order: [existing row (will update), fresh row (will insert)]
        rows = [
            {"id": fresh_id},  # conflicts → keeps existing_id
            {"id": str(uuid4())},  # inserts → keeps its own generated id
        ]
        session = _fake_pg_session(rows=[(existing_id,), (rows[1]["id"],)])
        with patch(
            "finance_sync.sync.upserts._upsert_stmt",
            return_value=MagicMock(),
        ) as mock_stmt:
            from finance_sync.sync.upserts import _run_upsert

            result = await _run_upsert(
                session, mock_stmt.return_value, [str(r["id"]) for r in rows]
            )

        assert result.inserted == 1
        assert result.updated == 1
        assert result.inserted_ids == (rows[1]["id"],)
        assert result.updated_ids == (existing_id,)

    async def test_pg_detection_skips_unchanged_rows(self) -> None:
        """Rows not returned by RETURNING (no value change) are absent."""
        from uuid import uuid4

        fresh_id = str(uuid4())
        session = _fake_pg_session(rows=[(fresh_id,)])
        with patch(
            "finance_sync.sync.upserts._upsert_stmt",
            return_value=MagicMock(),
        ) as mock_stmt:
            from finance_sync.sync.upserts import _run_upsert

            # Two input rows, only the fresh one returned (the other
            # conflicted with no change → not returned at all).
            result = await _run_upsert(
                session,
                mock_stmt.return_value,
                [fresh_id, str(uuid4())],
            )

        assert result.inserted == 1
        assert result.updated == 0
        assert result.total == 1
        assert result.inserted_ids == (fresh_id,)

    def test_stmt_has_change_detection_where_clause(self) -> None:
        """DO UPDATE is gated by IS DISTINCT FROM per update column.

        Without the gate an unchanged row would still be "updated" on
        every re-sync, bumping revision and re-emitting outbox events
        with the same idempotency key until the UNIQUE constraint
        aborts the run.
        """
        from finance_sync.models.transaction import Transaction

        stmt = _upsert_stmt(
            Transaction,
            [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "tenant_id": "tenant-1",
                    "provider_key": "trading212",
                    "connection_id": None,
                    "external_transaction_id": "tx-1",
                    "account_id": "account-1",
                    "amount": Decimal("1.00"),
                    "currency_code": "EUR",
                    "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "transaction_type": "deposit",
                    "revision": 1,
                }
            ],
            index_elements=(
                "tenant_id",
                "provider_key",
                "connection_id",
                "external_transaction_id",
            ),
            update_columns=("amount", "currency_code"),
            revision_column="revision",
        )
        compiled = str(
            stmt.compile(
                dialect=__import__("sqlalchemy").dialects.postgresql.dialect()
            )
        )
        assert "IS DISTINCT FROM" in compiled
        # Any changed mutable field must permit the update; requiring every
        # field to differ would make normal partial updates no-ops.
        assert " OR " in compiled
        assert "revision +" in compiled  # revision increments on change
        assert "ON CONFLICT" in compiled
        assert "DO UPDATE" in compiled

    def test_stmt_holding_no_revision_column(self) -> None:
        """Holdings upsert has no revision bump (no revision column)."""
        from finance_sync.models.holding import Holding

        stmt = _upsert_stmt(
            Holding,
            [
                {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "tenant_id": "tenant-1",
                    "account_id": "account-1",
                    "security_id": "security-1",
                    "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "source": "provider_sync",
                    "quantity": Decimal(2),
                    "currency_code": "EUR",
                }
            ],
            index_elements=(
                "tenant_id",
                "account_id",
                "security_id",
                "observed_at",
                "source",
            ),
            update_columns=("quantity", "market_value"),
        )
        compiled = str(
            stmt.compile(
                dialect=__import__("sqlalchemy").dialects.postgresql.dialect()
            )
        )
        assert "IS DISTINCT FROM" in compiled
        assert "revision" not in compiled
        assert "DO UPDATE" in compiled


class TestTransactionPersistenceBatch:
    @pytest.fixture
    def persistence(self):
        from finance_sync.sync.persistence import TransactionPersistence

        return TransactionPersistence("tenant-1")

    def _txn(
        self, external_id: str, amount: str = "1.00"
    ) -> CanonicalTransactionData:
        return CanonicalTransactionData(
            provider_key="trading212",
            external_transaction_id=external_id,
            external_account_id="account-ext-1",
            amount=Decimal(amount),
            currency_code="EUR",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            transaction_type="deposit",
            status="booked",
        )

    async def test_batch_mixed_inserts_updates_associate_correctly(
        self, persistence
    ) -> None:
        """Interleaved inserts/updates map to the right outbox events.

        PostgreSQL returns rows in VALUES order; the emitter must map
        each returned id back to its input row via the generated-id
        membership test, not by positional prefix (which would mislabel
        an updated row as created when an unchanged row was skipped).
        """
        from finance_sync.sync import persistence as persistence_module

        fresh_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        existing_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        # VALUES order: [insert, update, update(unchanged → not returned)]
        rows = [
            {"id": fresh_id},
            {"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
            {"id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
        ]
        session = _fake_pg_session(rows=[(fresh_id,), (existing_id,)])
        uow = SimpleNamespace(session=session)
        created = AsyncMock()
        updated = AsyncMock()
        with (
            patch.object(
                persistence_module,
                "outbox_entity_created",
                created,
            ),
            patch.object(
                persistence_module,
                "outbox_entity_updated",
                updated,
            ),
        ):
            # _emit_batch_outbox is what we're testing; hand it a mixed
            # result directly.
            result = UpsertResult(
                inserted_ids=(fresh_id,),
                updated_ids=(existing_id,),
            )
            await persistence._emit_batch_outbox(
                uow,
                [self._txn("tx-1"), self._txn("tx-2"), self._txn("tx-3")],
                result,
                [row["id"] for row in rows],
            )

        created.assert_awaited_once()
        assert created.await_args is not None
        # The created event must carry the *inserted* row's id (fresh_id)
        assert created.await_args.kwargs["entity_id"] == fresh_id
        updated.assert_awaited_once()
        assert updated.await_args is not None
        # The updated event carries the pre-existing id of the updated row
        assert updated.await_args.kwargs["entity_id"] == existing_id

    async def test_batch_emits_no_events_when_nothing_changed(
        self, persistence
    ) -> None:
        """A no-op re-sync (all conflicts unchanged) emits zero events."""
        from finance_sync.sync import persistence as persistence_module

        fresh_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        rows = [{"id": fresh_id}]
        session = _fake_pg_session(rows=[])  # nothing returned
        uow = SimpleNamespace(session=session)
        created = AsyncMock()
        updated = AsyncMock()
        with (
            patch.object(
                persistence_module,
                "outbox_entity_created",
                created,
            ),
            patch.object(
                persistence_module,
                "outbox_entity_updated",
                updated,
            ),
        ):
            await persistence._emit_batch_outbox(
                uow,
                [self._txn("tx-1")],
                UpsertResult(inserted_ids=(), updated_ids=()),
                [row["id"] for row in rows],
            )

        created.assert_not_awaited()
        updated.assert_not_awaited()

    async def test_batch_uses_pg_path_and_emits_outbox(
        self, persistence
    ) -> None:
        from finance_sync.sync import persistence as persistence_module

        # The generated id is created via ``uuid.uuid4()`` inside the
        # batch method; pin it so the fake PG result returns the same id
        # (which classifies the row as INSERTED).
        fixed_id = "11111111-1111-4111-8111-111111111111"
        session = _fake_pg_session(rows=[(fixed_id,)])
        uow = SimpleNamespace(session=session)
        created = AsyncMock()
        updated = AsyncMock()
        with (
            patch("uuid.uuid4", return_value=fixed_id),
            patch.object(
                persistence_module,
                "outbox_entity_created",
                created,
            ),
            patch.object(
                persistence_module,
                "outbox_entity_updated",
                updated,
            ),
        ):
            count = await persistence.persist_transactions_batch(
                uow,
                [self._txn("tx-1")],
                "account-1",
                security_ids=["security-1"],
                connection_id="connection-1",
            )

        assert count == 1
        created.assert_awaited_once()
        await_args = created.await_args
        assert await_args is not None
        assert await_args.kwargs["entity_type"] == "transaction"
        assert await_args.kwargs["entity_id"] == fixed_id
        updated.assert_not_awaited()
        # The PG upsert must have been executed
        session.execute.assert_awaited_once()

    async def test_batch_falls_back_to_per_row_on_sqlite(
        self, persistence
    ) -> None:
        session = _fake_sqlite_session()
        uow = SimpleNamespace(session=session)
        with (
            patch.object(
                persistence,
                "persist_transaction",
                new=AsyncMock(
                    side_effect=lambda *a, **k: SimpleNamespace(
                        id=f"id-{a[1].external_transaction_id}"
                    )
                ),
            ) as per_row,
            patch(
                "finance_sync.sync.persistence.outbox_entity_created",
                new=AsyncMock(),
            ) as created,
        ):
            count = await persistence.persist_transactions_batch(
                uow,
                [self._txn("tx-1"), self._txn("tx-2")],
                "account-1",
                security_ids=["security-1", "security-2"],
                connection_id="connection-1",
            )

        assert count == 2
        assert per_row.await_count == 2
        # The per-row fallback emits outbox events itself; the batch
        # method must NOT emit again (double-publish would violate the
        # outbox idempotency-key unique constraint on the same run).
        created.assert_not_awaited()


class TestHoldingPersistenceBatch:
    @pytest.fixture
    def persistence(self):
        from finance_sync.sync.persistence import HoldingPersistence

        return HoldingPersistence("tenant-1")

    def _holding(self) -> CanonicalHoldingData:
        return CanonicalHoldingData(
            provider_key="trading212",
            external_account_id="account-ext-1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            quantity=Decimal(2),
            security_reference=SecurityReference(isin="IE00BK5BQT80"),
            market_value=Decimal(220),
            currency_code="EUR",
        )

    async def test_batch_uses_pg_path_and_emits_outbox(
        self, persistence
    ) -> None:
        from finance_sync.sync import persistence as persistence_module

        fixed_id = "22222222-2222-4222-8222-222222222222"
        session = _fake_pg_session(rows=[(fixed_id,)])
        uow = SimpleNamespace(session=session)
        created = AsyncMock()
        updated = AsyncMock()
        with (
            patch("uuid.uuid4", return_value=fixed_id),
            patch.object(
                persistence_module,
                "outbox_entity_created",
                created,
            ),
            patch.object(
                persistence_module,
                "outbox_entity_updated",
                updated,
            ),
        ):
            count = await persistence.persist_holdings_batch(
                uow,
                [self._holding()],
                "account-1",
                security_ids=["security-1"],
            )

        assert count == 1
        created.assert_awaited_once()
        await_args = created.await_args
        assert await_args is not None
        assert await_args.kwargs["entity_id"] == fixed_id
        updated.assert_not_awaited()
        session.execute.assert_awaited_once()

    async def test_batch_falls_back_to_per_row_on_sqlite(
        self, persistence
    ) -> None:
        session = _fake_sqlite_session()
        uow = SimpleNamespace(session=session)
        with patch.object(
            persistence,
            "persist_holding",
            new=AsyncMock(
                side_effect=lambda *a, **k: SimpleNamespace(id="h-1")
            ),
        ) as per_row:
            count = await persistence.persist_holdings_batch(
                uow,
                [self._holding(), self._holding()],
                "account-1",
                security_ids=["security-1", "security-2"],
            )

        assert count == 2
        assert per_row.await_count == 2

    async def test_security_ids_length_mismatch_raises(
        self, persistence
    ) -> None:
        session = _fake_sqlite_session()
        uow = SimpleNamespace(session=session)
        with pytest.raises(ValueError):
            await persistence.persist_holdings_batch(
                uow,
                [self._holding()],
                "account-1",
                security_ids=[],
            )
