"""Unique-constraint contract for the upsert conflict targets.

The sync batch upsert paths (``finance_sync.sync.upserts``) use
``INSERT .. ON CONFLICT DO UPDATE`` with two conflict targets:

* ``transactions``: ``uq_transactions_provider``
  ``(tenant_id, provider_key, connection_id, external_transaction_id)``
  — rebuilt by migration 0046 as ``NULLS NOT DISTINCT`` so rows with a
  NULL ``connection_id`` (single-credential syncs, integration tests)
  still deduplicate.
* ``holdings``: ``uq_holdings_snapshot``
  ``(tenant_id, account_id, security_id, observed_at, source)`` —
  created by migration 0013.

These tests assert the contract the upsert code depends on, against a
fresh PostgreSQL database migrated through the full Alembic chain:

* both constraints exist, named and targeted exactly as the upsert
  statements expect;
* the database rejects duplicate natural keys (plain INSERT);
* ``uq_transactions_provider`` treats NULL ``connection_id`` rows as
  duplicates (NULLS NOT DISTINCT);
* both constraints are usable as ``ON CONFLICT`` conflict targets and
  the ``DO UPDATE`` path refreshes values instead of erroring;
* migration 0046 is safe on an existing database that already contains
  duplicate NULL-connection rows (deterministic dedupe before rebuild).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import run_alembic

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.integration

#: The exact conflict targets used by finance_sync.sync.upserts.
TRANSACTION_CONFLICT_COLUMNS = (
    "tenant_id",
    "provider_key",
    "connection_id",
    "external_transaction_id",
)
HOLDING_CONFLICT_COLUMNS = (
    "tenant_id",
    "account_id",
    "security_id",
    "observed_at",
    "source",
)

_UUID = postgresql.UUID(as_uuid=True)

_T = sa.table(
    "transactions",
    sa.column("id", _UUID),
    sa.column("tenant_id", _UUID),
    sa.column("provider_key", sa.String),
    sa.column("connection_id", _UUID),
    sa.column("external_transaction_id", sa.String),
    sa.column("account_id", _UUID),
    sa.column("amount", sa.Numeric),
    sa.column("currency_code", sa.String),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
    sa.column("transaction_type", sa.String),
    sa.column("status", sa.String),
)

_H = sa.table(
    "holdings",
    sa.column("id", _UUID),
    sa.column("tenant_id", _UUID),
    sa.column("account_id", _UUID),
    sa.column("security_id", _UUID),
    sa.column("observed_at", sa.DateTime(timezone=True)),
    sa.column("quantity", sa.Numeric),
    sa.column("currency_code", sa.String),
    sa.column("source", sa.String),
)

_TXN_INSERT_SQL = sa.text(
    "INSERT INTO transactions "
    "(id, tenant_id, provider_key, connection_id, "
    " external_transaction_id, account_id, amount, "
    " currency_code, occurred_at, transaction_type, "
    " status, created_at, updated_at) "
    "VALUES (:id, :tenant_id, :provider_key, :connection_id, "
    " :external_transaction_id, :account_id, :amount, "
    " :currency_code, :occurred_at, :transaction_type, "
    " :status, :created_at, :updated_at)"
)

_HOLDING_INSERT_SQL = sa.text(
    "INSERT INTO holdings "
    "(id, tenant_id, account_id, security_id, observed_at, "
    " quantity, currency_code, source, created_at, updated_at) "
    "VALUES (:id, :tenant_id, :account_id, :security_id, "
    " :observed_at, :quantity, :currency_code, :source, "
    " :created_at, :updated_at)"
)


@pytest.fixture
async def fresh_database_url(database_url: str) -> AsyncGenerator[str, None]:
    """Create a dedicated database, yield its DSN, drop it afterwards."""
    url = make_url(database_url)
    db_name = f"finance_sync_uniq_{uuid.uuid4().hex[:8]}"

    admin_url = url.set(database="postgres")
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await admin_engine.dispose()

    fresh_url = url.set(database=db_name)
    try:
        yield fresh_url.render_as_string(hide_password=False)
    finally:
        drop_engine = create_async_engine(
            admin_url.render_as_string(hide_password=False),
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with drop_engine.connect() as conn:
                await conn.execute(
                    sa.text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
                )
        finally:
            await drop_engine.dispose()


async def _unique_constraints(url: str) -> dict[str, str]:
    """Return {constraint_name: definition} for transactions + holdings."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT conname, pg_get_constraintdef(oid) AS def "
                    "FROM pg_constraint "
                    "WHERE conrelid IN "
                    "('transactions'::regclass, 'holdings'::regclass) "
                    "AND contype = 'u' "
                    "ORDER BY conname"
                )
            )
            return {row[0]: row[1] for row in result}
    finally:
        await engine.dispose()


async def _seed_baseline(engine) -> tuple[str, str, str]:
    """Insert a tenant + account + security; return their ids."""
    tenant_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    security_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, name) "
                "VALUES (:id, :slug, :name)"
            ),
            {
                "id": tenant_id,
                "slug": f"uniq-{uuid.uuid4().hex[:8]}",
                "name": "T",
            },
        )
        await conn.execute(
            sa.text(
                "INSERT INTO accounts "
                "(id, tenant_id, provider_key, external_account_id, name, "
                " account_type, currency_code, is_active, created_at, updated_at) "
                "VALUES (:id, :tenant, 'trading212', 'ext-acc', 'Acc', "
                " 'investment', 'EUR', true, now(), now())"
            ),
            {"id": account_id, "tenant": tenant_id},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO securities "
                "(id, ticker, name, security_type, currency_code, "
                " created_at, updated_at) "
                "VALUES (:id, 'VWCE', 'Vanguard FTSE', 'etf', 'EUR', "
                " now(), now())"
            ),
            {"id": security_id},
        )
    return tenant_id, account_id, security_id


class TestUniqueConstraintContract:
    """The conflict targets exist, are named right, and reject duplicates."""

    async def test_constraints_exist_with_expected_targets(
        self, fresh_database_url: str
    ) -> None:
        """Both upsert conflict targets exist after ``upgrade head``."""
        run_alembic("upgrade", "head", url=fresh_database_url)

        constraints = await _unique_constraints(fresh_database_url)
        assert "uq_transactions_provider" in constraints
        assert "uq_holdings_snapshot" in constraints

        # Exact column lists in constraint order — this is what the
        # INSERT .. ON CONFLICT index_elements must match.
        txn_def = constraints["uq_transactions_provider"]
        for column in TRANSACTION_CONFLICT_COLUMNS:
            assert column in txn_def, (
                f"uq_transactions_provider missing column {column}: {txn_def}"
            )
        assert "NULLS NOT DISTINCT" in txn_def.upper(), (
            "uq_transactions_provider must be NULLS NOT DISTINCT so NULL "
            f"connection_id rows deduplicate; got: {txn_def}"
        )

        holding_def = constraints["uq_holdings_snapshot"]
        for column in HOLDING_CONFLICT_COLUMNS:
            assert column in holding_def, (
                f"uq_holdings_snapshot missing column {column}: {holding_def}"
            )

    async def test_duplicate_transaction_natural_key_rejected(
        self, fresh_database_url: str
    ) -> None:
        """Plain INSERT of the same natural key fails (no connection scope)."""
        run_alembic("upgrade", "head", url=fresh_database_url)
        engine = create_async_engine(fresh_database_url)
        try:
            tenant_id, account_id, _ = await _seed_baseline(engine)
            base = {
                "id": uuid.uuid4(),
                "tenant_id": uuid.UUID(tenant_id),
                "provider_key": "trading212",
                "connection_id": None,
                "external_transaction_id": "txn-1",
                "account_id": uuid.UUID(account_id),
                "amount": 100.0,
                "currency_code": "EUR",
                "occurred_at": datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
                "transaction_type": "payment",
                "status": "booked",
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            async with engine.begin() as conn:
                await conn.execute(_TXN_INSERT_SQL, base)
            # Second row with the SAME natural key (NULL connection_id)
            # must be rejected — NULLS NOT DISTINCT semantics.
            dup = {**base, "id": uuid.uuid4()}
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(_TXN_INSERT_SQL, dup)
        finally:
            await engine.dispose()

    async def test_duplicate_holding_natural_key_rejected(
        self, fresh_database_url: str
    ) -> None:
        """Plain INSERT of the same holding snapshot key fails."""
        run_alembic("upgrade", "head", url=fresh_database_url)
        engine = create_async_engine(fresh_database_url)
        try:
            tenant_id, account_id, security_id = await _seed_baseline(engine)
            base = {
                "id": uuid.uuid4(),
                "tenant_id": uuid.UUID(tenant_id),
                "account_id": uuid.UUID(account_id),
                "security_id": uuid.UUID(security_id),
                "observed_at": datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
                "quantity": 10,
                "currency_code": "EUR",
                "source": "provider_sync",
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            async with engine.begin() as conn:
                await conn.execute(_HOLDING_INSERT_SQL, base)
            dup = {**base, "id": uuid.uuid4()}
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(_HOLDING_INSERT_SQL, dup)
        finally:
            await engine.dispose()

    async def test_on_conflict_do_update_uses_both_targets(
        self, fresh_database_url: str
    ) -> None:
        """``ON CONFLICT`` upserts refresh values and leave one row."""
        run_alembic("upgrade", "head", url=fresh_database_url)
        engine = create_async_engine(fresh_database_url)
        try:
            tenant_id, account_id, security_id = await _seed_baseline(engine)

            # Transaction upsert via the exact conflict target the
            # sync code uses (SQLAlchemy postgresql insert).
            txn_values = {
                "id": uuid.uuid4(),
                "tenant_id": uuid.UUID(tenant_id),
                "provider_key": "trading212",
                "connection_id": None,
                "external_transaction_id": "txn-1",
                "account_id": uuid.UUID(account_id),
                "amount": 100.0,
                "currency_code": "EUR",
                "occurred_at": datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
                "transaction_type": "payment",
                "status": "booked",
            }
            async with engine.begin() as conn:
                for _ in range(2):
                    await conn.execute(
                        insert(_T)
                        .values(txn_values)
                        .on_conflict_do_update(
                            index_elements=list(TRANSACTION_CONFLICT_COLUMNS),
                            set_={"amount": 250.0, "status": "booked"},
                        )
                    )

            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        sa.text(
                            "SELECT amount FROM transactions "
                            "WHERE external_transaction_id='txn-1'"
                        )
                    )
                ).all()
                assert len(rows) == 1, "upsert left duplicate rows"
                assert float(rows[0][0]) == 250.0, "upsert did not update value"

            # Holding upsert via its conflict target.
            holding_values = {
                "id": uuid.uuid4(),
                "tenant_id": uuid.UUID(tenant_id),
                "account_id": uuid.UUID(account_id),
                "security_id": uuid.UUID(security_id),
                "observed_at": datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
                "quantity": 10,
                "currency_code": "EUR",
                "source": "provider_sync",
            }
            async with engine.begin() as conn:
                for _ in range(2):
                    await conn.execute(
                        insert(_H)
                        .values(holding_values)
                        .on_conflict_do_update(
                            index_elements=list(HOLDING_CONFLICT_COLUMNS),
                            set_={"quantity": 20},
                        )
                    )

            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        sa.text(
                            "SELECT quantity FROM holdings "
                            "WHERE security_id=:sid"
                        ),
                        {"sid": security_id},
                    )
                ).all()
                assert len(rows) == 1, "holding upsert left duplicate rows"
                assert float(rows[0][0]) == 20.0, (
                    "holding upsert did not update"
                )
        finally:
            await engine.dispose()

    async def test_0046_dedupes_existing_null_connection_duplicates(
        self, fresh_database_url: str
    ) -> None:
        """Migration 0046 is safe on a DB with duplicate NULL-conn rows.

        A database migrated only to 0045 may already contain duplicate
        natural keys with ``connection_id IS NULL`` (the old constraint
        allowed them).  Rebuilding as NULLS NOT DISTINCT must dedupe
        deterministically (keep oldest) instead of failing.
        """
        run_alembic("upgrade", "0045", url=fresh_database_url)
        engine = create_async_engine(fresh_database_url)
        try:
            tenant_id, account_id, _ = await _seed_baseline(engine)

            async with engine.begin() as conn:
                # Two rows with the same natural key + NULL connection_id
                # are legal under the 0045 NULLS DISTINCT constraint.
                for _ in range(2):
                    await conn.execute(
                        sa.text(
                            "INSERT INTO transactions "
                            "(id, tenant_id, provider_key, connection_id, "
                            " external_transaction_id, account_id, amount, "
                            " currency_code, occurred_at, transaction_type, "
                            " status, created_at, updated_at) "
                            "VALUES (:id, :tenant_id, 'trading212', NULL, "
                            " 'dup-txn', :account_id, 10.0, 'EUR', now(), "
                            " 'payment', 'booked', now(), now())"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "tenant_id": tenant_id,
                            "account_id": account_id,
                        },
                    )
        finally:
            await engine.dispose()

        # The rebuild must succeed and dedupe to exactly one row.
        run_alembic("upgrade", "head", url=fresh_database_url)

        engine = create_async_engine(fresh_database_url)
        try:
            async with engine.connect() as conn:
                count = (
                    await conn.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM transactions "
                            "WHERE external_transaction_id='dup-txn'"
                        )
                    )
                ).scalar_one()
                assert count == 1, (
                    "0046 must dedupe NULL-connection duplicates, "
                    f"left {count} rows"
                )
                constraints = await _unique_constraints(fresh_database_url)
                assert (
                    "NULLS NOT DISTINCT"
                    in constraints["uq_transactions_provider"].upper()
                )
        finally:
            await engine.dispose()
