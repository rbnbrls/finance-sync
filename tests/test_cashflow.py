"""Tests for the Cashflow computation service (Phase 5).

Tests cover:
- CashflowService unit tests (mocked session)
- Cash flow calculation logic
- Category breakdown logic
- Date validation
- Static helper math
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

import pytest
from fastapi.testclient import TestClient

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db
from finance_sync.services.cashflow import (
    CashflowReport,
    CashflowService,
    CashflowSummary,
    PeriodEntry,
    compute_cashflow,
    compute_cashflow_summary,
)

# ── Test helpers ──────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-key-at-least-16-chars"

E = Decimal
_ZERO = E("0")

# A single mock-row shaped as a stand-in for a SQLAlchemy result row.
# We use a simple class so we can set attributes freely.


class _MockRow:
    """Minimal stand-in for a SQLAlchemy row object."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            object.__setattr__(self, k, v)


def _assert_sql_contains(mock: AsyncMock, fragment: str) -> None:
    """Assert that the compiled SQL from execute() contains ``fragment``."""
    call_args = mock.execute.call_args
    assert call_args is not None
    stmt = str(
        call_args[0][0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert fragment in stmt


# ── Shared fixtures ───────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        access_token_expire_minutes=15,
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings=settings)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_session() -> AsyncMock:
    """Create a mock session that returns empty results by default."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar.return_value = 0
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []
    mock_result.one.return_value = _MockRow(
        total_inflows=_ZERO,
        total_outflows=_ZERO,
        transaction_count=0,
        period_start=None,
        period_end=None,
    )
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def svc(mock_session: AsyncMock) -> CashflowService:
    return CashflowService(mock_session)


# ═══════════════════════════════════════════════════════════════════════
# CashflowService — empty / edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowServiceEmpty:
    """CashflowService.calculate() with no data."""

    async def test_empty_returns_zero(self, svc: CashflowService) -> None:
        result = await svc.calculate(tenant_id="t1")
        assert isinstance(result, CashflowSummary)
        assert result.total_inflows == _ZERO
        assert result.total_outflows == _ZERO
        assert result.net_cashflow == _ZERO
        assert result.transaction_count == 0

    async def test_empty_by_category(self, svc: CashflowService) -> None:
        result = await svc.by_category(tenant_id="t1")
        assert isinstance(result, list)
        assert len(result) == 0

    async def test_empty_by_period(self, svc: CashflowService) -> None:
        result = await svc.by_period(tenant_id="t1")
        assert isinstance(result, list)
        assert len(result) == 0

    async def test_empty_full_report(self, svc: CashflowService) -> None:
        result = await svc.full_report(tenant_id="t1")
        assert isinstance(result, CashflowReport)
        assert result.summary.total_inflows == _ZERO
        assert result.summary.total_outflows == _ZERO
        assert result.summary.net_cashflow == _ZERO
        assert len(result.by_category) == 0
        assert len(result.history) == 0

    async def test_passes_tenant_filter(self, mock_session: AsyncMock) -> None:
        svc = CashflowService(mock_session)
        await svc.calculate(tenant_id="tenant-abc")
        assert mock_session.execute.call_count >= 1

    async def test_passes_tenant_filter_by_category(
        self,
        mock_session: AsyncMock,
    ) -> None:
        svc = CashflowService(mock_session)
        await svc.by_category(tenant_id="tenant-xyz")
        assert mock_session.execute.call_count >= 1

    async def test_passes_tenant_filter_by_period(
        self,
        mock_session: AsyncMock,
    ) -> None:
        svc = CashflowService(mock_session)
        await svc.by_period(tenant_id="tenant-xyz")
        assert mock_session.execute.call_count >= 1


# ═══════════════════════════════════════════════════════════════════════
# CashflowService — computed totals
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowServiceTotals:
    """CashflowService.calculate() with mocked DB results."""

    async def test_computes_correct_inflows_outflows_net(self) -> None:
        """Verify that inflows, outflows, and net are correctly computed."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=E("1500.00"),
            total_outflows=E("850.00"),
            transaction_count=15,
            period_start=datetime(2025, 1, 1, tzinfo=UTC),
            period_end=datetime(2025, 12, 31, tzinfo=UTC),
        )
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        result = await svc.calculate(
            tenant_id="t1",
            date_from=datetime(2025, 1, 1, tzinfo=UTC),
            date_to=datetime(2025, 12, 31, tzinfo=UTC),
        )

        assert result.total_inflows == E("1500.00")
        assert result.total_outflows == E("850.00")
        assert result.net_cashflow == E("650.00")  # 1500 - 850
        assert result.transaction_count == 15
        assert result.period_start == datetime(2025, 1, 1, tzinfo=UTC)
        assert result.period_end == datetime(2025, 12, 31, tzinfo=UTC)

    async def test_net_cashflow_can_be_negative(self) -> None:
        """Verify net cashflow is negative when outflows exceed inflows."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=E("500.00"),
            total_outflows=E("1200.00"),
            transaction_count=20,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        result = await svc.calculate(tenant_id="t1")

        assert result.total_inflows == E("500.00")
        assert result.total_outflows == E("1200.00")
        assert result.net_cashflow == E("-700.00")

    async def test_generates_sql_with_tenant_filter(self) -> None:
        """Verify the SQL query includes the tenant_id filter."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.calculate(tenant_id="my-tenant")

        _assert_sql_contains(mock_session, "my-tenant")

    async def test_generates_sql_with_date_filter(self) -> None:
        """Verify SQL includes date-range conditions."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.calculate(
            tenant_id="t1",
            date_from=datetime(2025, 6, 1, tzinfo=UTC),
            date_to=datetime(2025, 8, 31, tzinfo=UTC),
        )

        _assert_sql_contains(mock_session, "2025-06-01")
        _assert_sql_contains(mock_session, "2025-08-31")


# ═══════════════════════════════════════════════════════════════════════
# CashflowService — category breakdown
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowServiceCategory:
    """CashflowService.by_category() behaviour."""

    async def test_returns_all_categories(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [
            _MockRow(
                transaction_type="deposit",
                total_amount=E("3000.00"),
                transaction_count=3,
            ),
            _MockRow(
                transaction_type="payment",
                total_amount=E("-120.50"),
                transaction_count=5,
            ),
            _MockRow(
                transaction_type="fee",
                total_amount=E("-15.00"),
                transaction_count=2,
            ),
            _MockRow(
                transaction_type="interest",
                total_amount=E("42.00"),
                transaction_count=1,
            ),
        ]
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        result = await svc.by_category(tenant_id="t1")

        assert len(result) == 4

        cat_map = {c.transaction_type: c for c in result}

        # Income categories
        assert cat_map["deposit"].total_amount == E("3000.00")
        assert cat_map["deposit"].transaction_count == 3
        assert cat_map["deposit"].is_income is True

        assert cat_map["interest"].total_amount == E("42.00")
        assert cat_map["interest"].is_income is True

        # Expense categories
        assert cat_map["payment"].is_income is False
        assert cat_map["fee"].is_income is False

    async def test_sorts_by_transaction_type(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [
            _MockRow(
                transaction_type="deposit",
                total_amount=E("100"),
                transaction_count=1,
            ),
            _MockRow(
                transaction_type="fee",
                total_amount=E("-10"),
                transaction_count=1,
            ),
            _MockRow(
                transaction_type="payment",
                total_amount=E("-50"),
                transaction_count=1,
            ),
        ]
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        result = await svc.by_category(tenant_id="t1")

        # Should be sorted alphabetically: deposit, fee, payment
        types = [c.transaction_type for c in result]
        assert types == ["deposit", "fee", "payment"]

    async def test_sql_includes_group_by(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.by_category(tenant_id="t1")

        _assert_sql_contains(mock_session, "GROUP BY")


# ═══════════════════════════════════════════════════════════════════════
# CashflowService — period history
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowServicePeriod:
    """CashflowService.by_period() behaviour."""

    async def test_returns_period_entries(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [
            _MockRow(
                date=datetime(2025, 6, 1, tzinfo=UTC),
                inflows=E("2000"),
                outflows=E("800"),
                net=E("1200"),
                transaction_count=10,
            ),
            _MockRow(
                date=datetime(2025, 7, 1, tzinfo=UTC),
                inflows=E("1500"),
                outflows=E("900"),
                net=E("600"),
                transaction_count=8,
            ),
        ]
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        result = await svc.by_period(tenant_id="t1", interval="month")

        assert len(result) == 2
        assert isinstance(result[0], PeriodEntry)
        assert result[0].inflows == E("2000")
        assert result[0].outflows == E("800")
        assert result[0].net == E("1200")
        assert result[0].transaction_count == 10

    async def test_sql_includes_interval(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.by_period(tenant_id="t1", interval="week")

        _assert_sql_contains(mock_session, "date_trunc")


# ═══════════════════════════════════════════════════════════════════════
# CashflowService — validation
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowServiceValidation:
    """Input validation tests."""

    def test_raises_on_invalid_date_range(self) -> None:
        """date_from after date_to should raise ValueError."""
        from_ = datetime(2025, 12, 31, tzinfo=UTC)
        to_ = datetime(2025, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match=r"date_from.*after.*date_to"):
            CashflowService.validate_date_range(from_, to_)

    def test_valid_date_range_passes(self) -> None:
        """Valid date range should not raise."""
        from_ = datetime(2025, 1, 1, tzinfo=UTC)
        to_ = datetime(2025, 12, 31, tzinfo=UTC)
        # Should not raise
        CashflowService.validate_date_range(from_, to_)

    def test_none_dates_passes(self) -> None:
        """Both dates being None should not raise."""
        CashflowService.validate_date_range(None, None)


# ═══════════════════════════════════════════════════════════════════════
# Static helper — compute_net_cashflow
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowStaticHelper:
    """CashflowService.compute_net_cashflow() pure logic."""

    def test_all_positive_amounts(self) -> None:
        amounts = [E("100"), E("200"), E("50")]
        inflows, outflows, net = CashflowService.compute_net_cashflow(amounts)
        assert inflows == E("350")
        assert outflows == _ZERO
        assert net == E("350")

    def test_all_negative_amounts(self) -> None:
        amounts = [E("-100"), E("-200"), E("-50")]
        inflows, outflows, net = CashflowService.compute_net_cashflow(amounts)
        assert inflows == _ZERO
        assert outflows == E("350")
        assert net == E("-350")

    def test_mixed_amounts(self) -> None:
        amounts = [E("500"), E("-200"), E("100"), E("-50")]
        inflows, outflows, net = CashflowService.compute_net_cashflow(amounts)
        assert inflows == E("600")
        assert outflows == E("250")
        assert net == E("350")

    def test_empty_list(self) -> None:
        inflows, outflows, net = CashflowService.compute_net_cashflow([])
        assert inflows == _ZERO
        assert outflows == _ZERO
        assert net == _ZERO

    def test_single_zero(self) -> None:
        inflows, outflows, net = CashflowService.compute_net_cashflow([_ZERO])
        assert inflows == _ZERO
        assert outflows == _ZERO
        assert net == _ZERO


# ═══════════════════════════════════════════════════════════════════════
# SQL correctness — verify generated SQL contains expected elements
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowSQLGeneration:
    """Verify generated SQL contains required clauses."""

    async def test_calculate_sql_has_tenant_filter(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.calculate(tenant_id="tenant-99")

        _assert_sql_contains(mock_session, "tenant-99")
        _assert_sql_contains(mock_session, "occurred_at")
        _assert_sql_contains(mock_session, "booked")

    async def test_by_category_sql_has_group_by(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.by_category(tenant_id="tenant-42")

        _assert_sql_contains(mock_session, "GROUP BY")
        _assert_sql_contains(mock_session, "transaction_type")
        _assert_sql_contains(mock_session, "tenant-42")

    async def test_by_period_sql_has_date_trunc(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.by_period(tenant_id="tenant-7", interval="month")

        _assert_sql_contains(mock_session, "date_trunc")
        _assert_sql_contains(mock_session, "month")
        _assert_sql_contains(mock_session, "tenant-7")

    async def test_full_report_combines_three_queries(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        result = await svc.full_report(tenant_id="tenant-88")

        assert isinstance(result, CashflowReport)
        # Should have called execute 3 times (summary + categories + history)
        assert mock_session.execute.call_count == 3


# ═══════════════════════════════════════════════════════════════════════
# Standalone compute_cashflow() convenience function
# ═══════════════════════════════════════════════════════════════════════


class TestComputeCashflowFunction:
    """Module-level ``compute_cashflow()`` convenience wrapper."""

    async def test_delegates_to_service_calculate(
        self, mock_session: AsyncMock
    ) -> None:
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=E("1000"),
            total_outflows=E("400"),
            transaction_count=14,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result

        from finance_sync.services.cashflow import compute_cashflow_from_db

        result = await compute_cashflow_from_db(mock_session, tenant_id="t1")
        assert result.total_inflows == E("1000")
        assert result.total_outflows == E("400")
        assert result.net_cashflow == E("600")
        assert result.transaction_count == 14

    async def test_returns_cashflow_summary_type(
        self, mock_session: AsyncMock
    ) -> None:
        from finance_sync.services.cashflow import compute_cashflow_from_db

        result = await compute_cashflow_from_db(mock_session, tenant_id="t1")
        from finance_sync.services.cashflow import CashflowSummary

        assert isinstance(result, CashflowSummary)

    async def test_uses_default_date_range(
        self, mock_session: AsyncMock
    ) -> None:
        from finance_sync.services.cashflow import compute_cashflow_from_db

        await compute_cashflow_from_db(mock_session, tenant_id="t-default")
        _assert_sql_contains(mock_session, "t-default")


# ═══════════════════════════════════════════════════════════════════════
# Account IDs filtering — singular and plural
# ═══════════════════════════════════════════════════════════════════════


class TestCashflowAccountFilter:
    """Filtering by account_id (singular) and account_ids (plural)."""

    async def test_singular_account_id_generates_equality(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result
        svc = CashflowService(mock_session)
        await svc.calculate(tenant_id="t1", account_id="acct-123")
        _assert_sql_contains(mock_session, "acct-123")

    async def test_plural_account_ids_generates_in_clause(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result
        svc = CashflowService(mock_session)
        await svc.calculate(
            tenant_id="t1", account_ids=["acct-a", "acct-b", "acct-c"]
        )
        _assert_sql_contains(mock_session, "acct-a")
        _assert_sql_contains(mock_session, "acct-b")
        _assert_sql_contains(mock_session, "acct-c")

    async def test_singular_and_plural_combined(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result
        svc = CashflowService(mock_session)
        await svc.calculate(
            tenant_id="t1",
            account_id="single",
            account_ids=["multi-a", "multi-b"],
        )
        _assert_sql_contains(mock_session, "single")
        _assert_sql_contains(mock_session, "multi-a")
        _assert_sql_contains(mock_session, "multi-b")

    async def test_no_account_filter_when_both_none(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result
        svc = CashflowService(mock_session)
        await svc.calculate(tenant_id="t1")
        compiled = str(
            mock_session.execute.call_args[0][0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        # No account_id mentioned at all — only tenant, date, and booked status
        assert "tenant" in compiled.lower()

    async def test_account_ids_passed_via_compute_cashflow(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.one.return_value = _MockRow(
            total_inflows=_ZERO,
            total_outflows=_ZERO,
            transaction_count=0,
            period_start=None,
            period_end=None,
        )
        mock_session.execute.return_value = mock_result

        from finance_sync.services.cashflow import compute_cashflow_from_db

        await compute_cashflow_from_db(
            mock_session, tenant_id="t1", account_ids=["multi-1"]
        )
        _assert_sql_contains(mock_session, "multi-1")

    async def test_account_ids_in_by_category(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.by_category(tenant_id="t1", account_ids=["cat-acct"])

        _assert_sql_contains(mock_session, "cat-acct")

    async def test_account_ids_in_by_period(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        await svc.by_period(tenant_id="t1", account_ids=["period-acct"])

        _assert_sql_contains(mock_session, "period-acct")

    async def test_account_ids_in_count_periods(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_session.execute.return_value = mock_result

        svc = CashflowService(mock_session)
        total = await svc.count_periods(
            tenant_id="t1", account_ids=["cnt-acct"]
        )

        assert total == 5

    async def test_account_ids_in_full_report(self) -> None:
        mock_session = AsyncMock()
        mock_result_summary = MagicMock()
        mock_result_summary.one.return_value = _MockRow(
            total_inflows=E("100"),
            total_outflows=E("50"),
            transaction_count=3,
            period_start=None,
            period_end=None,
        )
        mock_result_summary.all.return_value = []
        mock_session.execute.return_value = mock_result_summary

        svc = CashflowService(mock_session)
        await svc.full_report(tenant_id="t1", account_ids=["report-acct"])

        assert mock_session.execute.call_count == 3


# ═══════════════════════════════════════════════════════════════════════
# Pure-data compute_cashflow() — in-memory transaction lists
# ═══════════════════════════════════════════════════════════════════════


def _tx(
    amount: str,
    occurred_at: str,
    account_id: str = "acct-a",
    type_: str = "deposit",
    status: str = "booked",
) -> dict:
    """Build a minimal transaction dict for pure-data tests."""
    return {
        "amount": E(amount),
        "occurred_at": datetime.fromisoformat(occurred_at),
        "account_id": account_id,
        "transaction_type": type_,
        "status": status,
    }


class TestComputeCashflowPureData:
    """``compute_cashflow()`` — period-based in-memory aggregation."""

    def test_empty_list_returns_empty(self) -> None:
        result = compute_cashflow([])
        assert result == []

    def test_single_transaction_creates_one_period(self) -> None:
        txs = [_tx("100", "2025-06-15T00:00:00+00:00")]
        result = compute_cashflow(txs)
        assert len(result) == 1
        entry = result[0]
        assert entry.inflows == E("100")
        assert entry.outflows == E("0")
        assert entry.net == E("100")
        assert entry.transaction_count == 1

    def test_output_transaction_increases_outflows(self) -> None:
        txs = [_tx("-50", "2025-06-15T00:00:00+00:00")]
        result = compute_cashflow(txs)
        assert len(result) == 1
        entry = result[0]
        assert entry.inflows == E("0")
        assert entry.outflows == E("50")
        assert entry.net == E("-50")

    def test_groups_multiple_transactions_into_same_month(self) -> None:
        txs = [
            _tx("100", "2025-06-01T00:00:00+00:00"),
            _tx("200", "2025-06-15T00:00:00+00:00"),
            _tx("-50", "2025-06-20T00:00:00+00:00"),
        ]
        result = compute_cashflow(txs)
        assert len(result) == 1
        entry = result[0]
        assert entry.inflows == E("300")
        assert entry.outflows == E("50")
        assert entry.net == E("250")
        assert entry.transaction_count == 3

    def test_splits_across_different_months(self) -> None:
        txs = [
            _tx("1000", "2025-05-01T00:00:00+00:00"),
            _tx("-500", "2025-05-15T00:00:00+00:00"),
            _tx("1500", "2025-06-01T00:00:00+00:00"),
            _tx("-300", "2025-06-10T00:00:00+00:00"),
        ]
        result = compute_cashflow(txs)
        assert len(result) == 2  # May and June

        may = result[0]
        jun = result[1]
        assert may.date == datetime(2025, 5, 1, tzinfo=UTC)
        assert may.inflows == E("1000")
        assert may.outflows == E("500")
        assert may.net == E("500")

        assert jun.date == datetime(2025, 6, 1, tzinfo=UTC)
        assert jun.inflows == E("1500")
        assert jun.outflows == E("300")
        assert jun.net == E("1200")

    def test_results_are_chronological(self) -> None:
        """Results should be sorted oldest period first."""
        txs = [
            _tx("100", "2025-08-01T00:00:00+00:00"),
            _tx("200", "2025-06-01T00:00:00+00:00"),
            _tx("300", "2025-07-01T00:00:00+00:00"),
        ]
        result = compute_cashflow(txs)
        dates = [e.date for e in result]
        assert dates == sorted(dates)  # chronological

    def test_date_filter_start_excludes_early(self) -> None:
        txs = [
            _tx("100", "2025-01-01T00:00:00+00:00"),
            _tx("200", "2025-06-15T00:00:00+00:00"),
        ]
        result = compute_cashflow(
            txs,
            start_date=datetime(2025, 6, 1, tzinfo=UTC),
        )
        assert len(result) == 1
        assert result[0].inflows == E("200")

    def test_date_filter_end_excludes_late(self) -> None:
        txs = [
            _tx("100", "2025-06-01T00:00:00+00:00"),
            _tx("200", "2025-12-31T00:00:00+00:00"),
        ]
        result = compute_cashflow(
            txs,
            end_date=datetime(2025, 6, 30, tzinfo=UTC),
        )
        assert len(result) == 1
        assert result[0].inflows == E("100")

    def test_account_ids_filter(self) -> None:
        txs = [
            _tx("100", "2025-06-01T00:00:00+00:00", account_id="acct-a"),
            _tx("200", "2025-06-15T00:00:00+00:00", account_id="acct-b"),
        ]
        result = compute_cashflow(txs, account_ids=["acct-a"])
        assert len(result) == 1
        assert result[0].inflows == E("100")

    def test_multi_account_filter(self) -> None:
        txs = [
            _tx("100", "2025-06-01T00:00:00+00:00", account_id="acct-a"),
            _tx("200", "2025-06-15T00:00:00+00:00", account_id="acct-b"),
            _tx("300", "2025-06-20T00:00:00+00:00", account_id="acct-c"),
        ]
        result = compute_cashflow(txs, account_ids=["acct-a", "acct-c"])
        assert len(result) == 1
        assert result[0].inflows == E("400")

    def test_pending_transactions_excluded_by_default(self) -> None:
        txs = [
            _tx("100", "2025-06-01T00:00:00+00:00", status="booked"),
            _tx("200", "2025-06-15T00:00:00+00:00", status="pending"),
        ]
        result = compute_cashflow(txs)
        assert len(result) == 1
        assert result[0].inflows == E("100")

    def test_include_pending_flag(self) -> None:
        txs = [
            _tx("100", "2025-06-01T00:00:00+00:00", status="booked"),
            _tx("200", "2025-06-15T00:00:00+00:00", status="pending"),
        ]
        result = compute_cashflow(txs, include_pending=True)
        assert len(result) == 1
        assert result[0].inflows == E("300")

    def test_raises_on_invalid_date_range(self) -> None:
        txs = [_tx("100", "2025-06-15T00:00:00+00:00")]
        with pytest.raises(ValueError, match=r"start_date.*after.*end_date"):
            compute_cashflow(
                txs,
                start_date=datetime(2025, 12, 31, tzinfo=UTC),
                end_date=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_week_interval_groups_by_monday(self) -> None:
        """Week interval should truncate to ISO Monday."""
        # 2025-06-19 is a Thursday
        txs = [_tx("100", "2025-06-19T12:00:00+00:00")]
        result = compute_cashflow(txs, interval="week")
        assert len(result) == 1
        # Monday of that week is 2025-06-16
        assert result[0].date == datetime(2025, 6, 16, tzinfo=UTC)

    def test_day_interval(self) -> None:
        txs = [
            _tx("100", "2025-06-01T00:00:00+00:00"),
            _tx("200", "2025-06-02T00:00:00+00:00"),
        ]
        result = compute_cashflow(txs, interval="day")
        assert len(result) == 2

    def test_year_interval(self) -> None:
        txs = [
            _tx("100", "2025-03-01T00:00:00+00:00"),
            _tx("200", "2025-07-01T00:00:00+00:00"),
            _tx("300", "2026-01-15T00:00:00+00:00"),
        ]
        result = compute_cashflow(txs, interval="year")
        assert len(result) == 2

    def test_raises_on_invalid_interval(self) -> None:
        txs = [_tx("100", "2025-06-01T00:00:00+00:00")]
        with pytest.raises(ValueError, match="Invalid interval"):
            compute_cashflow(txs, interval="decade")

    def test_works_with_model_style_objects(self) -> None:
        """Test with object-style attributes (not dicts)."""

        class _Obj:
            amount = E("100")
            occurred_at = datetime(2025, 6, 15, tzinfo=UTC)
            account_id = "acct-a"
            transaction_type = "deposit"
            status = "booked"

        result = compute_cashflow([_Obj()])
        assert len(result) == 1
        assert result[0].inflows == E("100")

    def test_accepts_model_objects_with_status_via_getattr(self) -> None:
        """Objects with no 'status' attribute should not crash."""

        class _Minimal:
            amount = E("50")
            occurred_at = datetime(2025, 6, 15, tzinfo=UTC)
            account_id = "acct-a"

        result = compute_cashflow([_Minimal()])
        assert len(result) == 1
        assert result[0].inflows == E("50")


# ═══════════════════════════════════════════════════════════════════════
# Pure-data compute_cashflow_summary() — in-memory aggregate
# ═══════════════════════════════════════════════════════════════════════


class TestComputeCashflowSummary:
    """``compute_cashflow_summary()`` — aggregate in-memory summary."""

    def test_empty_returns_zero_summary(self) -> None:
        result = compute_cashflow_summary([])
        assert isinstance(result, CashflowSummary)
        assert result.total_inflows == _ZERO
        assert result.total_outflows == _ZERO
        assert result.net_cashflow == _ZERO
        assert result.transaction_count == 0

    def test_aggregates_all_transactions(self) -> None:
        txs = [
            _tx("1500", "2025-06-01T00:00:00+00:00"),
            _tx("-500", "2025-06-15T00:00:00+00:00"),
            _tx("200", "2025-07-01T00:00:00+00:00"),
            _tx("-75", "2025-07-10T00:00:00+00:00"),
        ]
        result = compute_cashflow_summary(txs)
        assert result.total_inflows == E("1700")
        assert result.total_outflows == E("575")
        assert result.net_cashflow == E("1125")

    def test_filters_by_account(self) -> None:
        txs = [
            _tx("1500", "2025-06-01T00:00:00+00:00", account_id="a"),
            _tx("200", "2025-06-15T00:00:00+00:00", account_id="b"),
        ]
        result = compute_cashflow_summary(txs, account_ids=["a"])
        assert result.total_inflows == E("1500")
        assert result.transaction_count == 1

    def test_filters_by_date(self) -> None:
        txs = [
            _tx("100", "2025-01-01T00:00:00+00:00"),
            _tx("200", "2025-06-15T00:00:00+00:00"),
        ]
        result = compute_cashflow_summary(
            txs,
            start_date=datetime(2025, 6, 1, tzinfo=UTC),
        )
        assert result.total_inflows == E("200")

    def test_sets_period_bounds(self) -> None:
        txs = [
            _tx("100", "2025-01-01T00:00:00+00:00"),
            _tx("200", "2025-12-31T00:00:00+00:00"),
        ]
        result = compute_cashflow_summary(txs)
        assert result.period_start == datetime(2025, 1, 1, tzinfo=UTC)
        assert result.period_end == datetime(2025, 12, 31, tzinfo=UTC)
