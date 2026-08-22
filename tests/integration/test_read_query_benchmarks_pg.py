"""PostgreSQL query-budget benchmarks for the read facade."""

# pyright: basic

from __future__ import annotations

import os
import platform
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from uuid import UUID, uuid5

import pytest
from sqlalchemy import text

from finance_sync.models import (
    Account,
    Holding,
    Security,
    SecurityPrice,
    Tenant,
)
from finance_sync.models.enums import (
    AccountType,
    HoldingSource,
    SecurityType,
)
from finance_sync.models.transaction import Transaction
from finance_sync.services.read.analytics import AnalyticsReadService
from finance_sync.services.read.benchmarking import (
    ReadBenchmarkResult,
    write_benchmark_report,
)
from finance_sync.services.read.budgets import READ_QUERY_BUDGETS, QueryBudget
from finance_sync.services.read.portfolio import PortfolioReadService
from finance_sync.services.read.prices import fetch_latest_daily_prices
from finance_sync.services.read.query_counter import QueryCounter
from finance_sync.services.read.securities import SecuritiesReadService

pytestmark = pytest.mark.integration

_NAMESPACE = UUID("4f9f5e7e-f125-5fd2-8f98-4ebce8d99531")
_OBSERVED_AT = datetime(2025, 1, 15, 12, tzinfo=UTC)


def _id(label: str) -> UUID:
    return uuid5(_NAMESPACE, label)


async def _seed_dataset(
    session, *, holding_count: int, account_count: int
) -> tuple[str, list[str]]:
    tenant_id = _id(f"tenant-{holding_count}")
    session.add(
        Tenant(
            id=tenant_id,
            slug=f"benchmark-{holding_count}",
            name="Read benchmark",
        )
    )
    await session.flush()
    accounts = [
        Account(
            id=_id(f"account-{holding_count}-{index}"),
            tenant_id=tenant_id,
            provider_key="benchmark",
            external_account_id=f"account-{index}",
            name=f"Benchmark account {index}",
            account_type=AccountType.BROKERAGE
            if index
            else AccountType.CHECKING,
            currency_code="EUR",
            current_balance=Decimal("1000.00"),
            available_balance=Decimal("900.00"),
        )
        for index in range(account_count)
    ]
    securities = [
        Security(
            id=_id(f"security-{holding_count}-{index}"),
            ticker=f"BM{index:04d}",
            name=f"Benchmark security {index}",
            security_type=SecurityType.ETF,
            currency_code="EUR",
        )
        for index in range(holding_count)
    ]
    session.add_all([*accounts, *securities])
    await session.flush()

    holdings = [
        Holding(
            tenant_id=tenant_id,
            account_id=accounts[index % account_count].id,
            security_id=security.id,
            observed_at=_OBSERVED_AT,
            quantity=Decimal("2.00000000"),
            cost_basis=Decimal("100.00"),
            currency_code="EUR",
            price=None,
            market_value=None,
            source=HoldingSource.PROVIDER_SYNC,
        )
        for index, security in enumerate(securities)
    ]
    # Some prices are deliberately missing and some are stale. This keeps
    # fallback and freshness behaviour present in every benchmark dataset.
    prices = [
        SecurityPrice(
            security_id=security.id,
            timestamp=(
                _OBSERVED_AT - timedelta(days=180)
                if index % 5 == 0
                else _OBSERVED_AT
            ),
            price_close=Decimal("55.00") + index,
            price_open=Decimal("54.00") + index,
            price_high=Decimal("56.00") + index,
            price_low=Decimal("53.00") + index,
            source="benchmark",
            interval="1d",
            currency_code="EUR",
        )
        for index, security in enumerate(securities)
        if index % 3 != 0
    ]
    transactions = [
        Transaction(
            tenant_id=tenant_id,
            provider_key="benchmark",
            external_transaction_id=f"transaction-{holding_count}-{index}",
            account_id=accounts[index % account_count].id,
            amount=Decimal("100.00") if index % 2 == 0 else Decimal("-25.00"),
            currency_code="EUR",
            occurred_at=_OBSERVED_AT - timedelta(days=index),
            transaction_type="deposit",
            status="booked",
        )
        for index in range(account_count * 2)
    ]
    session.add_all([*holdings, *prices, *transactions])
    await session.commit()
    return str(tenant_id), [str(security.id) for security in securities]


async def _measure(
    session,
    engine,
    *,
    dataset: str,
    holding_count: int,
    account_count: int,
    operation: str,
    callback,
) -> ReadBenchmarkResult:
    budget: QueryBudget = READ_QUERY_BUDGETS[operation]
    started = perf_counter()
    with QueryCounter(engine) as counter:
        await callback()
    budget.assert_within(counter.queries)
    return ReadBenchmarkResult(
        dataset=dataset,
        holding_count=holding_count,
        account_count=account_count,
        operation=operation,
        budget=budget.max_queries,
        query_count=counter.queries,
        latency_ms=round((perf_counter() - started) * 1000, 3),
    )


@pytest.mark.asyncio
async def test_read_query_budgets_against_deterministic_postgres_datasets(
    session_factory, pg_engine
) -> None:
    """All read operations stay within budget at both benchmark sizes."""
    results: list[ReadBenchmarkResult] = []
    postgres_version: str | None = None

    for holding_count, account_count in ((100, 5), (1000, 20)):
        async with session_factory() as session:
            tenant_id, security_ids = await _seed_dataset(
                session,
                holding_count=holding_count,
                account_count=account_count,
            )
            dataset = f"holdings-{holding_count}"
            portfolio = PortfolioReadService(session)
            securities = SecuritiesReadService(session)
            analytics = AnalyticsReadService(session)
            calls = {
                "portfolio": lambda svc=portfolio, tid=tenant_id: (
                    svc.get_portfolio(tid)
                ),
                "holdings": lambda svc=portfolio, tid=tenant_id, count=holding_count: (
                    svc.get_holdings(tid, limit=count)
                ),
                "securities": lambda svc=securities, count=holding_count: (
                    svc.list_securities(limit=count)
                ),
                "latest_prices": lambda ids=security_ids: (
                    fetch_latest_daily_prices(session, ids)
                ),
                "net_worth": lambda svc=analytics, tid=tenant_id: (
                    svc.get_net_worth(tid)
                ),
                "cashflow": lambda svc=analytics, tid=tenant_id: (
                    svc.get_cashflow(tid)
                ),
            }
            for operation, callback in calls.items():
                results.append(
                    await _measure(
                        session,
                        pg_engine,
                        dataset=dataset,
                        holding_count=holding_count,
                        account_count=account_count,
                        operation=operation,
                        callback=callback,
                    )
                )
            if postgres_version is None:
                postgres_version = (
                    await session.execute(text("select version()"))
                ).scalar_one()

    artifact_path = os.environ.get("READ_BENCHMARK_ARTIFACT")
    if artifact_path:
        write_benchmark_report(
            artifact_path,
            results=results,
            postgres_version=postgres_version or "unknown",
            python_version=platform.python_version(),
        )


@pytest.mark.asyncio
async def test_read_query_budget_rejects_artificial_n_plus_one(
    session_factory, pg_engine
) -> None:
    """The gate fails when latest security prices are fetched one-by-one."""
    async with session_factory() as session:
        _tenant_id, security_ids = await _seed_dataset(
            session, holding_count=100, account_count=5
        )
        service = SecuritiesReadService(session)
        with QueryCounter(pg_engine) as counter:
            for security_id in security_ids[:6]:
                await service.get_security_prices(security_id, limit=1)
        with pytest.raises(AssertionError, match="budget is 3"):
            READ_QUERY_BUDGETS["securities"].assert_within(counter.queries)
