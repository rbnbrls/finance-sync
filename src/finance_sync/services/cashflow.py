"""Cashflow computation service — aggregate transactions by category and period.

Provides business-logic methods for computing net cash flow (income minus
expenses) from canonical Transaction records. Supports:

- Overall summary (total inflows / outflows / net)
- Category breakdown (by transaction_type)
- Historical time-series grouped by configurable date intervals
- Date-range filtering

Pattern follows the ``PerformanceService`` convention: a standalone service
class that takes an ``AsyncSession`` and returns Pydantic response models.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, select

from finance_sync.models.enums import TransactionStatus
from finance_sync.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ── Constants ──────────────────────────────────────────────────────────

E = Decimal
_ZERO = E("0")

INCOME_TYPES: frozenset[str] = frozenset(
    {
        "deposit",
        "interest",
        "dividend",
    }
)

EXPENSE_TYPES: frozenset[str] = frozenset(
    {
        "payment",
        "purchase",
        "fee",
        "withdrawal",
    }
)

# ── Response models ────────────────────────────────────────────────────


class CashflowSummary(BaseModel):
    """Aggregate cash-flow figures for a period."""

    total_inflows: E = E("0")
    total_outflows: E = E("0")
    net_cashflow: E = E("0")
    transaction_count: int = 0
    currency_code: str = "EUR"
    period_start: datetime | None = None
    period_end: datetime | None = None


class CategoryBreakdown(BaseModel):
    """Cash-flow figures for a single transaction category."""

    transaction_type: str
    total_amount: E
    transaction_count: int
    is_income: bool = False


class PeriodEntry(BaseModel):
    """Single period's cash flow in a time-series."""

    date: datetime
    inflows: E
    outflows: E
    net: E
    transaction_count: int = 0


class CategoryPeriodEntry(BaseModel):
    """Single period's cash flow broken down by category."""

    date: datetime
    categories: list[CategoryBreakdown]
    total_inflows: E
    total_outflows: E
    net: E


class CashflowReport(BaseModel):
    """Full cashflow report combining summary, categories, and history."""

    summary: CashflowSummary
    by_category: list[CategoryBreakdown] = Field(default_factory=list)
    history: list[PeriodEntry] = Field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────


def _expr(*conditions: Any) -> Any:
    """Wrap conditions in ``and_()``, handling the empty case."""
    if not conditions:
        return True
    return and_(*conditions)


# ── Standalone convenience functions (DB-based) ─────────────────────────


async def compute_cashflow_from_db(
    session: AsyncSession,
    tenant_id: str,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    account_ids: list[str] | None = None,
    currency_code: str | None = None,
) -> CashflowSummary:
    """Compute aggregate cash-flow summary via a DB session —
    convenience wrapper.

    This is a module-level shorthand that creates a ``CashflowService``
    internally and calls ``.calculate()``.  Use it when you only need the
    top-level summary without touching the service class directly.

    Parameters
    ----------
    session :
        Async SQLAlchemy session.
    tenant_id :
        Tenant to scope the query to.
    date_from, date_to :
        Optional date range.  Defaults to last 365 days.
    account_ids :
        Optional list of account UUIDs to filter by.
    currency_code :
        Optional currency filter (e.g. ``"EUR"``).

    Returns
    -------
    CashflowSummary
        Aggregate inflows, outflows, net, and transaction count.
    """
    svc = CashflowService(session)
    return await svc.calculate(
        tenant_id,
        date_from=date_from,
        date_to=date_to,
        account_ids=account_ids,
        currency_code=currency_code,
    )


# ── Pure-data functions (in-memory transaction lists) ──────────────────


def _get_amount(obj: Any) -> E:
    """Extract Decimal amount from a transaction-like object.

    Supports both model instances (``.amount``) and dicts (``obj["amount"]``).
    """
    if isinstance(obj, dict):
        return E(str(obj.get("amount", _ZERO)))
    return E(str(getattr(obj, "amount", _ZERO)))


def _get_date(obj: Any) -> datetime:
    """Extract datetime from a transaction-like object's
    occurred_at field."""
    if isinstance(obj, dict):
        val = obj.get("occurred_at", datetime.now(UTC))
    else:
        val = getattr(obj, "occurred_at", datetime.now(UTC))
    if isinstance(val, str):
        val = datetime.fromisoformat(val)
    return val


def _get_account_id(obj: Any) -> str:
    """Extract account id from a transaction-like object."""
    if isinstance(obj, dict):
        return str(obj.get("account_id", ""))
    return str(getattr(obj, "account_id", ""))


def _truncate_to_period(dt: datetime, interval: str = "month") -> datetime:
    """Truncate a datetime to the start of the given period.

    Supports ``'day'``, ``'week'`` (ISO Monday), ``'month'``, and ``'year'``.
    """
    if interval == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "week":
        # ISO weekday: Monday=1 … Sunday=7
        days_since_monday = dt.weekday()
        truncated = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta as td

        return truncated - td(days=days_since_monday)
    if interval == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if interval == "year":
        return dt.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    msg = (
        f"Invalid interval '{interval}'. Must be one of: day, week, month, year"
    )
    raise ValueError(msg)


def compute_cashflow(
    transactions: list[Any],
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    account_ids: list[str] | None = None,
    interval: str = "month",
    include_pending: bool = False,
) -> list[PeriodEntry]:
    """Compute cash-flow time-series from an in-memory list of transactions.

    This is a pure-data alternative to :meth:`CashflowService.by_period` —
    it operates on a list of transaction-like objects (model instances,
    namedtuples, or dicts) without requiring a database session.

    Parameters
    ----------
    transactions :
        List of objects with ``.amount`` (``Decimal`` or numeric),
        ``.occurred_at`` (``datetime``), and optionally ``.account_id``
        (``str``) attributes.  Dicts with ``"amount"``, ``"occurred_at"``,
        and ``"account_id"`` keys are also accepted.
    start_date, end_date :
        Optional date range.  Only transactions whose ``occurred_at``
        falls within the range are included.
    account_ids :
        Optional list of account UUIDs to filter by.
    interval :
        Period interval — ``'day'``, ``'week'``, ``'month'`` (default),
        or ``'year'``.
    include_pending :
        If ``True``, include all transactions including those with
        non-booked status.  Default ``False`` (only include booked).

    Returns
    -------
    list[PeriodEntry]
        Ordered chronologically (oldest first).  Each entry has
        ``date``, ``inflows``, ``outflows``, ``net``, and
        ``transaction_count``.

    Raises
    ------
    ValueError
        If ``start_date`` is after ``end_date``.
    """
    if (
        start_date is not None
        and end_date is not None
        and start_date > end_date
    ):
        msg = (
            f"start_date ({start_date.isoformat()}) must not be after "
            f"end_date ({end_date.isoformat()})"
        )
        raise ValueError(msg)

    # ── Filter ──────────────────────────────────────────────────────
    filtered: list[Any] = []
    account_set: set[str] | None = set(account_ids) if account_ids else None

    for txn in transactions:
        dt = _get_date(txn)

        if start_date is not None and dt < start_date:
            continue
        if end_date is not None and dt > end_date:
            continue

        if account_set is not None:
            acct_id = _get_account_id(txn)
            if acct_id not in account_set:
                continue

        if not include_pending:
            status = (
                txn.get("status")
                if isinstance(txn, dict)
                else getattr(txn, "status", None)
            )
            if status is not None and str(status) != "booked":
                continue

        filtered.append(txn)

    if not filtered:
        return []

    # ── Group into periods ──────────────────────────────────────────
    periods: dict[datetime, dict[str, E | int]] = {}

    for txn in filtered:
        dt = _get_date(txn)
        period_key = _truncate_to_period(dt, interval)
        amt = _get_amount(txn)

        if period_key not in periods:
            periods[period_key] = {
                "inflows": _ZERO,
                "outflows": _ZERO,
                "count": 0,
            }

        entry = periods[period_key]
        if amt > _ZERO:
            entry["inflows"] = entry["inflows"] + amt  # type: ignore[operator]
        else:
            entry["outflows"] = entry["outflows"] + (-amt)  # type: ignore[operator]
        entry["count"] = entry["count"] + 1  # type: ignore[operator]

    # ── Build result sorted chronologically ─────────────────────────
    result: list[PeriodEntry] = []
    for key in sorted(periods):
        d = periods[key]
        inflows: E = d["inflows"]  # type: ignore[assignment]
        outflows: E = d["outflows"]  # type: ignore[assignment]
        count: int = d["count"]  # type: ignore[assignment]
        result.append(
            PeriodEntry(
                date=key,
                inflows=inflows,
                outflows=outflows,
                net=inflows - outflows,
                transaction_count=count,
            )
        )

    return result


def compute_cashflow_summary(
    transactions: list[Any],
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    account_ids: list[str] | None = None,
    include_pending: bool = False,
) -> CashflowSummary:
    """Compute aggregate cash-flow summary from an in-memory transaction list.

    Pure-data counterpart of :meth:`CashflowService.calculate`.
    Accepts the same input types as :func:`compute_cashflow`.

    Parameters
    ----------
    transactions :
        List of transaction-like objects (model instances or dicts).
    start_date, end_date :
        Optional date range.
    account_ids :
        Optional list of account UUIDs to filter by.
    include_pending :
        If ``True``, include pending transactions.  Default ``False``.

    Returns
    -------
    CashflowSummary
        Aggregate inflows, outflows, net, and transaction count.
    """
    periods = compute_cashflow(
        transactions,
        start_date=start_date,
        end_date=end_date,
        account_ids=account_ids,
        interval="day",
        include_pending=include_pending,
    )

    total_in = sum((p.inflows for p in periods), _ZERO)
    total_out = sum((p.outflows for p in periods), _ZERO)
    total_count = sum((p.transaction_count for p in periods), 0)

    dates = [p.date for p in periods]
    period_start = min(dates) if dates else None
    period_end = max(dates) if dates else None

    return CashflowSummary(
        total_inflows=total_in,
        total_outflows=total_out,
        net_cashflow=total_in - total_out,
        transaction_count=total_count,
        period_start=period_start,
        period_end=period_end,
    )


# ── Service ────────────────────────────────────────────────────────────


class CashflowService:
    """Computes cashflow analytics from Transaction records.

    Each method returns Pydantic response models.  Methods are async and
    operate on an async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Public API ────────────────────────────────────────────────────

    async def calculate(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_id: str | None = None,
        account_ids: list[str] | None = None,
        currency_code: str | None = None,
    ) -> CashflowSummary:
        """Compute aggregate cash-flow summary for a period.

        Returns total inflows, outflows, net, and transaction count
        for the given tenant (optionally filtered by date range,
        account(s), or currency).
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        conditions = self._base_conditions(tenant_id, start, end)
        self._add_account_filters(conditions, account_id, account_ids)
        if currency_code is not None:
            conditions.append(
                Transaction.currency_code == currency_code  # type: ignore[attr-defined]
            )

        inflow_expr = func.coalesce(
            func.sum(Transaction.amount).filter(
                Transaction.amount > 0  # type: ignore[attr-defined]
            ),
            _ZERO,
        ).label("total_inflows")

        outflow_expr = func.coalesce(
            func.sum(-Transaction.amount).filter(
                Transaction.amount < 0  # type: ignore[attr-defined]
            ),
            _ZERO,
        ).label("total_outflows")

        agg_q = (
            select(
                inflow_expr,
                outflow_expr,
                func.count().label("transaction_count"),
                func.min(Transaction.occurred_at).label("period_start"),  # type: ignore[attr-defined]
                func.max(Transaction.occurred_at).label("period_end"),  # type: ignore[attr-defined]
            )
            .select_from(Transaction)
            .where(_expr(*conditions))
        )
        result = await self._session.execute(agg_q)
        row = result.one()

        total_inflows = row.total_inflows or _ZERO
        total_outflows = row.total_outflows or _ZERO
        net_cashflow = total_inflows - total_outflows

        return CashflowSummary(
            total_inflows=total_inflows,
            total_outflows=total_outflows,
            net_cashflow=net_cashflow,
            transaction_count=row.transaction_count or 0,
            period_start=row.period_start,
            period_end=row.period_end,
        )

    async def by_category(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_id: str | None = None,
        account_ids: list[str] | None = None,
        transaction_type: str | None = None,
    ) -> list[CategoryBreakdown]:
        """Break down cash flow by transaction type (category).

        Groups booked transactions by ``transaction_type`` and returns
        the sum and count for each category.  Pass ``transaction_type``
        to filter to a single category (server-side).
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        conditions = self._base_conditions(tenant_id, start, end)
        self._add_account_filters(conditions, account_id, account_ids)
        if transaction_type is not None:
            conditions.append(
                Transaction.transaction_type == transaction_type  # type: ignore[attr-defined]
            )

        cat_q = (
            select(
                Transaction.transaction_type,  # type: ignore[attr-defined]
                func.sum(Transaction.amount).label("total_amount"),  # type: ignore[attr-defined]
                func.count().label("transaction_count"),
            )
            .where(_expr(*conditions))
            .group_by(Transaction.transaction_type)  # type: ignore[attr-defined]
            .order_by(Transaction.transaction_type)  # type: ignore[attr-defined]
        )
        result = await self._session.execute(cat_q)
        rows = result.all()

        return [
            CategoryBreakdown(
                transaction_type=str(row.transaction_type),
                total_amount=row.total_amount or _ZERO,
                transaction_count=row.transaction_count or 0,
                is_income=(row.transaction_type in INCOME_TYPES),
            )
            for row in rows
        ]

    async def by_period(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_id: str | None = None,
        account_ids: list[str] | None = None,
        transaction_type: str | None = None,
        interval: str = "day",
        limit: int = 365,
        offset: int = 0,
    ) -> list[PeriodEntry]:
        """Return cash-flow time-series grouped by a date interval.

        Parameters
        ----------
        interval : str
            SQL date-truncation unit — ``'day'``, ``'week'``, ``'month'``,
            or ``'year'``.  Passed directly to ``date_trunc()``.
        transaction_type : str | None
            Optional filter to include only a specific transaction
            type/category.
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        conditions = self._base_conditions(tenant_id, start, end)
        self._add_account_filters(conditions, account_id, account_ids)
        if transaction_type is not None:
            conditions.append(
                Transaction.transaction_type == transaction_type  # type: ignore[attr-defined]
            )

        date_col = func.date_trunc(interval, Transaction.occurred_at)  # type: ignore[attr-defined]

        inflow_expr = func.coalesce(
            func.sum(Transaction.amount).filter(
                Transaction.amount > 0  # type: ignore[attr-defined]
            ),
            _ZERO,
        ).label("inflows")

        outflow_expr = func.coalesce(
            func.sum(-Transaction.amount).filter(
                Transaction.amount < 0  # type: ignore[attr-defined]
            ),
            _ZERO,
        ).label("outflows")

        period_q = (
            select(
                date_col.label("date"),
                inflow_expr,
                outflow_expr,
                func.sum(Transaction.amount).label("net"),  # type: ignore[attr-defined]
                func.count().label("transaction_count"),
            )
            .where(_expr(*conditions))
            .group_by(date_col)
            .order_by(desc(date_col))
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(period_q)
        rows = result.all()

        return [
            PeriodEntry(
                date=row.date,
                inflows=row.inflows or _ZERO,
                outflows=row.outflows or _ZERO,
                net=row.net or _ZERO,
                transaction_count=row.transaction_count or 0,
            )
            for row in rows
        ]

    async def count_periods(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_id: str | None = None,
        account_ids: list[str] | None = None,
        transaction_type: str | None = None,
        interval: str = "day",
    ) -> int:
        """Count distinct periods matching the filters.

        Uses the same date-truncation interval as :meth:`by_period` so
        the count is consistent with the paginated query.
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        conditions = self._base_conditions(tenant_id, start, end)
        self._add_account_filters(conditions, account_id, account_ids)
        if transaction_type is not None:
            conditions.append(
                Transaction.transaction_type == transaction_type  # type: ignore[attr-defined]
            )

        date_col = func.date_trunc(interval, Transaction.occurred_at)  # type: ignore[attr-defined]
        count_q = (
            select(func.count(func.distinct(date_col)))
            .select_from(Transaction)
            .where(_expr(*conditions))
        )
        result = await self._session.execute(count_q)
        return result.scalar() or 0

    async def full_report(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_id: str | None = None,
        account_ids: list[str] | None = None,
        interval: str = "month",
        limit: int = 12,
    ) -> CashflowReport:
        """Produce a complete cashflow report with summary, categories,
        and history."""
        summary = await self.calculate(
            tenant_id,
            date_from=date_from,
            date_to=date_to,
            account_id=account_id,
            account_ids=account_ids,
        )
        categories = await self.by_category(
            tenant_id,
            date_from=date_from,
            date_to=date_to,
            account_id=account_id,
            account_ids=account_ids,
        )
        history = await self.by_period(
            tenant_id,
            date_from=date_from,
            date_to=date_to,
            account_id=account_id,
            account_ids=account_ids,
            interval=interval,
            limit=limit,
        )

        return CashflowReport(
            summary=summary,
            by_category=categories,
            history=history,
        )

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _add_account_filters(
        conditions: list[Any],
        account_id: str | None,
        account_ids: list[str] | None,
    ) -> None:
        """Append account-ID filter(s) to *conditions* in-place.

        Supports both singular (``account_id``) and plural
        (``account_ids``) parameters.  When both are provided the
        filters are combined via OR so that all specified accounts
        are included.
        """
        ids: list[str] = []
        if account_id is not None:
            ids.append(account_id)
        if account_ids is not None:
            ids.extend(account_ids)

        if len(ids) == 0:
            return
        if len(ids) == 1:
            conditions.append(
                Transaction.account_id == ids[0]  # type: ignore[attr-defined]
            )
        else:
            conditions.append(
                Transaction.account_id.in_(ids)  # type: ignore[attr-defined]
            )

    def _base_conditions(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Any]:
        """Return the standard WHERE clauses for cash-flow queries."""
        return [
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Transaction.occurred_at >= start,  # type: ignore[attr-defined]
            Transaction.occurred_at <= end,  # type: ignore[attr-defined]
            Transaction.status == TransactionStatus.BOOKED,  # type: ignore[attr-defined]
        ]

    @staticmethod
    def compute_net_cashflow(transactions: list[Any]) -> tuple[E, E, E]:
        """Static helper to compute net cash flow from an in-memory list.

        Parameters
        ----------
        transactions : list[Transaction]
            List of Transaction model instances (or any object with
            ``.amount`` as ``Decimal``).

        Returns
        -------
        tuple[Decimal, Decimal, Decimal]
            (total_inflows, total_outflows, net_cashflow)
        """
        total_in = _ZERO
        total_out = _ZERO

        for txn in transactions:
            amt = txn.amount if hasattr(txn, "amount") else txn
            if amt > _ZERO:
                total_in += amt
            else:
                total_out += -amt

        return total_in, total_out, total_in - total_out

    @staticmethod
    def validate_date_range(
        date_from: datetime | None, date_to: datetime | None
    ) -> None:
        """Validate that a date range is meaningful.

        Raises ``ValueError`` if ``date_from`` is after ``date_to``.
        """
        if (
            date_from is not None
            and date_to is not None
            and date_from > date_to
        ):
            msg = (
                f"date_from ({date_from.isoformat()}) must not be after "
                f"date_to ({date_to.isoformat()})"
            )
            raise ValueError(msg)
