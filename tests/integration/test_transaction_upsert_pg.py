"""Integration tests for TransactionRepository.upsert() against real PG.

The acceptance criteria for the upsert method:

* a new transaction (fresh natural key) is **inserted**;
* an existing transaction (same natural key, changed values) is
  **updated** in place — every fetched field is overwritten;
* re-upserting identical data leaves **exactly one row** and is a no-op
  (no revision bump, no ``updated_at`` churn);
* a NULL incoming optional field (e.g. ``security_id``) does **not**
  null out a previously-set value;
* the method participates in the caller's transaction — it must not
  commit or roll back on its own (verified by exercising it inside a
  ``UnitOfWork`` whose outer commit decides persistence).

The conflict target is ``uq_transactions_provider``
``(tenant_id, provider_key, connection_id, external_transaction_id)``
rebuilt as ``NULLS NOT DISTINCT`` by migration 0046, so NULL
``connection_id`` rows still deduplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from finance_sync.db.uow import UnitOfWork
from finance_sync.models import Account, Security, Tenant
from finance_sync.models.enums import (
    AccountType,
    SecurityType,
    TransactionStatus,
    TransactionType,
)

pytestmark = pytest.mark.integration

#: Required fields for a Transaction row (matching the ORM non-null
#: columns).  The upsert fills the natural-key columns itself; these are
#: the "fetched" values passed through ``values=``.
_TXN_FIELDS = {
    "amount": Decimal("100.00"),
    "currency_code": "EUR",
    "occurred_at": datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
    "transaction_type": TransactionType.DEPOSIT,
    "status": TransactionStatus.BOOKED,
}


async def _create_tenant_account(
    session_factory,
    *,
    provider_key: str = "trading212",
    external_account_id: str = "acc-ext-1",
) -> tuple[str, str]:
    """Create a tenant + account; return (tenant_id, account_id)."""
    async with session_factory() as session, UnitOfWork(session) as uow:
        tenant = await uow.tenants.add(Tenant(slug="upsert-t", name="Upsert"))
        account = await uow.accounts.add(
            Account(
                tenant_id=tenant.id,
                provider_key=provider_key,
                external_account_id=external_account_id,
                name="Upsert Account",
                account_type=AccountType.INVESTMENT,
                currency_code="EUR",
                current_balance=Decimal(0),
                available_balance=Decimal(0),
            )
        )
        return str(tenant.id), str(account.id)


async def _count(session_factory, tenant_id: str) -> int:
    """Count transactions for a tenant in a fresh session."""
    from sqlalchemy import func, select

    from finance_sync.models import Transaction

    async with session_factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.tenant_id == tenant_id  # type: ignore[attr-defined]
            )
        )
        return int(result.scalar_one())


class TestTransactionUpsert:
    """Acceptance: insert → update → no-duplicate idempotent re-run."""

    async def test_insert_then_update_then_no_duplicate(
        self, session_factory
    ) -> None:
        tenant_id, account_id = await _create_tenant_account(session_factory)
        natural = {
            "tenant_id": tenant_id,
            "provider_key": "trading212",
            "external_transaction_id": "txn-1",
            "connection_id": None,
        }

        # 1. First upsert inserts a new row.
        async with session_factory() as session, UnitOfWork(session) as uow:
            entity = await uow.transactions.upsert(
                **natural,
                account_id=account_id,
                values={**_TXN_FIELDS, "description": "initial"},
            )
            await uow.commit()
        assert entity.id is not None
        assert entity.amount == Decimal("100.00")
        assert entity.description == "initial"
        assert entity.revision == 1
        assert await _count(session_factory, tenant_id) == 1

        # 2. Re-upsert with changed values updates the same row.
        async with session_factory() as session, UnitOfWork(session) as uow:
            updated = await uow.transactions.upsert(
                **natural,
                account_id=account_id,
                values={
                    **_TXN_FIELDS,
                    "amount": Decimal("250.00"),
                    "description": "updated",
                    "status": TransactionStatus.REVERSED,
                },
            )
            await uow.commit()
        assert updated.id == entity.id, "upsert must update, not re-insert"
        assert updated.amount == Decimal("250.00")
        assert updated.description == "updated"
        assert updated.status == TransactionStatus.REVERSED
        assert updated.revision == 2, "revision bumps on real change"
        assert await _count(session_factory, tenant_id) == 1

        # 3. Re-upsert with identical data is a no-op — exactly one row,
        #    revision unchanged.
        async with session_factory() as session, UnitOfWork(session) as uow:
            same = await uow.transactions.upsert(
                **natural,
                account_id=account_id,
                values={
                    **_TXN_FIELDS,
                    "amount": Decimal("250.00"),
                    "description": "updated",
                    "status": TransactionStatus.REVERSED,
                },
            )
            await uow.commit()
        assert same.id == entity.id
        assert same.revision == 2, "no-op re-upsert must not bump revision"
        assert await _count(session_factory, tenant_id) == 1

    async def test_null_optional_field_does_not_null_existing(
        self, session_factory
    ) -> None:
        """A NULL incoming security_id must not unlink a resolved one."""
        tenant_id, account_id = await _create_tenant_account(session_factory)
        # A real security row so the FK constraint is satisfied.
        async with session_factory() as session, UnitOfWork(session) as uow:
            security = await uow.securities.add(
                Security(
                    ticker="VWCE",
                    name="Vanguard FTSE All-World",
                    security_type=SecurityType.ETF,
                    currency_code="EUR",
                )
            )
            security_id = str(security.id)
        natural = {
            "tenant_id": tenant_id,
            "provider_key": "trading212",
            "external_transaction_id": "txn-sec",
            "connection_id": None,
        }

        async with session_factory() as session, UnitOfWork(session) as uow:
            first = await uow.transactions.upsert(
                **natural,
                account_id=account_id,
                values={**_TXN_FIELDS, "security_id": security_id},
            )
            await uow.commit()
        assert str(first.security_id) == security_id

        # Re-upsert where the provider no longer reports a security
        # reference (NULL): the previously-linked id must be preserved.
        async with session_factory() as session, UnitOfWork(session) as uow:
            second = await uow.transactions.upsert(
                **natural,
                account_id=account_id,
                values={**_TXN_FIELDS, "security_id": None},
            )
            await uow.commit()
        assert str(second.security_id) == security_id
        assert second.revision == 1, "NULL not-reported field is not a change"
        assert await _count(session_factory, tenant_id) == 1

    async def test_upsert_participates_in_outer_transaction(
        self, session_factory
    ) -> None:
        """Without an outer commit the upsert leaves no row behind.

        The method must not commit or roll back by itself: the caller's
        UnitOfWork owns the transaction boundary.  When the outer block
        raises (rollback), nothing may persist.
        """
        tenant_id, account_id = await _create_tenant_account(session_factory)
        natural = {
            "tenant_id": tenant_id,
            "provider_key": "trading212",
            "external_transaction_id": "txn-rollback",
            "connection_id": None,
        }

        forced_error = RuntimeError("forced failure")
        with pytest.raises(RuntimeError, match="forced"):
            async with session_factory() as session, UnitOfWork(session) as uow:
                await uow.transactions.upsert(
                    **natural,
                    account_id=account_id,
                    values=_TXN_FIELDS,
                )
                raise forced_error

        assert await _count(session_factory, tenant_id) == 0

    async def test_connection_scoped_conflict_target(
        self, session_factory
    ) -> None:
        """Same external id under two connections yields two rows;
        same connection re-upserts into one."""
        tenant_id, account_id = await _create_tenant_account(session_factory)
        conn_a = str(uuid4())
        conn_b = str(uuid4())

        async with session_factory() as session, UnitOfWork(session) as uow:
            row_a = await uow.transactions.upsert(
                tenant_id=tenant_id,
                provider_key="trading212",
                external_transaction_id="txn-conn",
                account_id=account_id,
                connection_id=conn_a,
                values=_TXN_FIELDS,
            )
            await uow.commit()

        # Different connection = different natural key → new row.
        async with session_factory() as session, UnitOfWork(session) as uow:
            row_b = await uow.transactions.upsert(
                tenant_id=tenant_id,
                provider_key="trading212",
                external_transaction_id="txn-conn",
                account_id=account_id,
                connection_id=conn_b,
                values=_TXN_FIELDS,
            )
            await uow.commit()
        assert row_b.id != row_a.id
        assert await _count(session_factory, tenant_id) == 2

        # Same connection again → update, still one row for that key.
        async with session_factory() as session, UnitOfWork(session) as uow:
            row_a2 = await uow.transactions.upsert(
                tenant_id=tenant_id,
                provider_key="trading212",
                external_transaction_id="txn-conn",
                account_id=account_id,
                connection_id=conn_a,
                values={**_TXN_FIELDS, "amount": Decimal("9.99")},
            )
            await uow.commit()
        assert row_a2.id == row_a.id
        assert row_a2.amount == Decimal("9.99")
        assert await _count(session_factory, tenant_id) == 2
