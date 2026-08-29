"""Integration tests for HoldingRepository.upsert() against real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from finance_sync.db.uow import UnitOfWork
from finance_sync.models import Account, Holding, Security, Tenant
from finance_sync.models.enums import AccountType, SecurityType

pytestmark = pytest.mark.integration


async def _create_baseline(session_factory) -> tuple[str, str, str]:
    async with session_factory() as session, UnitOfWork(session) as uow:
        tenant = await uow.tenants.add(
            Tenant(slug="holding-upsert", name="Holding")
        )
        account = await uow.accounts.add(
            Account(
                tenant_id=tenant.id,
                provider_key="trading212",
                external_account_id="account-1",
                name="Investment",
                account_type=AccountType.INVESTMENT,
                currency_code="EUR",
                current_balance=Decimal(0),
                available_balance=Decimal(0),
            )
        )
        security = await uow.securities.add(
            Security(
                ticker="VWCE",
                name="Vanguard FTSE All-World",
                security_type=SecurityType.ETF,
                currency_code="EUR",
            )
        )
        return str(tenant.id), str(account.id), str(security.id)


class TestHoldingUpsert:
    async def test_insert_update_and_repeat_leaves_one_row(
        self, session_factory
    ) -> None:
        tenant_id, account_id, security_id = await _create_baseline(
            session_factory
        )
        natural = {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "security_id": security_id,
            "observed_at": datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
            "source": "provider_sync",
        }
        initial = {
            "quantity": Decimal(10),
            "cost_basis": Decimal(100),
            "cost_basis_currency": "EUR",
            "market_value": Decimal(120),
            "currency_code": "EUR",
            "price": Decimal(12),
            "price_currency": "EUR",
        }

        async with session_factory() as session, UnitOfWork(session) as uow:
            first = await uow.holdings.upsert(**natural, values=initial)
            await uow.commit()

        async with session_factory() as session, UnitOfWork(session) as uow:
            second = await uow.holdings.upsert(
                **natural,
                values={
                    **initial,
                    "quantity": Decimal(11),
                    "market_value": Decimal(132),
                },
            )
            await uow.commit()

        assert second.id == first.id
        assert second.quantity == Decimal("11.00000000")
        assert second.market_value == Decimal("132.00000000")

        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Holding)
                .where(Holding.tenant_id == tenant_id)
            )
        assert count == 1

    async def test_upsert_does_not_commit_or_rollback(
        self, session_factory
    ) -> None:
        tenant_id, account_id, security_id = await _create_baseline(
            session_factory
        )
        forced_error = RuntimeError("forced")
        with pytest.raises(RuntimeError, match="forced"):
            async with session_factory() as session, UnitOfWork(session) as uow:
                await uow.holdings.upsert(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    security_id=security_id,
                    observed_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
                    source="provider_sync",
                    values={"quantity": Decimal(1), "currency_code": "EUR"},
                )
                raise forced_error

        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Holding)
                .where(Holding.tenant_id == tenant_id)
            )
        assert count == 0
