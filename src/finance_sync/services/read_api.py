"""Service layer for read-only API endpoints.

Centralises all read queries (accounts, portfolios, net-worth, etc.)
so that API route handlers stay thin.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select

from finance_sync.models.account import Account
from finance_sync.models.balance import Balance
from finance_sync.models.holding import Holding
from finance_sync.models.security import Security
from finance_sync.models.security_price import SecurityPrice
from finance_sync.models.sync_run import SyncRun
from finance_sync.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ── Response DTOs ─────────────────────────────────────────────────────

E = Decimal


class AccountSummary(BaseModel):
    id: str
    name: str
    account_type: str
    account_subtype: str | None = None
    currency_code: str
    current_balance: E | None = None
    available_balance: E | None = None
    provider_key: str
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AccountDetailResponse(BaseModel):
    items: list[AccountSummary]
    total: int
    limit: int
    offset: int


class TransactionResponse(BaseModel):
    id: str
    account_id: str
    security_id: str | None = None
    amount: E
    currency_code: str
    occurred_at: datetime
    booked_at: datetime | None = None
    description: str | None = None
    transaction_type: str
    status: str
    provider_key: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TransactionListResponse(BaseModel):
    items: list[TransactionResponse]
    total: int
    limit: int
    offset: int


class BalanceResponse(BaseModel):
    id: str
    account_id: str
    observed_at: datetime
    balance_kind: str
    amount: E
    currency_code: str
    source: str
    created_at: datetime | None = None


class BalanceListResponse(BaseModel):
    items: list[BalanceResponse]
    total: int
    limit: int
    offset: int


class SecurityInfo(BaseModel):
    id: str
    isin: str | None = None
    figi: str | None = None
    ticker: str | None = None
    name: str
    security_type: str
    currency_code: str
    latest_price: E | None = None
    latest_price_currency: str | None = None
    latest_price_timestamp: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SecurityListResponse(BaseModel):
    items: list[SecurityInfo]
    total: int
    limit: int
    offset: int


class SecurityPriceResponse(BaseModel):
    id: str
    security_id: str
    timestamp: datetime
    price_open: E | None = None
    price_high: E | None = None
    price_low: E | None = None
    price_close: E | None = None
    volume: E | None = None
    source: str
    interval: str
    currency_code: str


class SecurityPriceListResponse(BaseModel):
    items: list[SecurityPriceResponse]
    total: int
    limit: int
    offset: int


class HoldingBreakdown(BaseModel):
    security_id: str
    ticker: str | None = None
    security_name: str
    security_type: str
    quantity: E
    cost_basis: E | None = None
    cost_basis_currency: str | None = None
    market_value: E | None = None
    price: E | None = None
    price_currency: str | None = None
    currency_code: str
    unrealised_pl: E | None = None
    unrealised_pl_pct: E | None = None


class AccountPortfolioBreakdown(BaseModel):
    account_id: str
    account_name: str
    account_type: str
    holdings: list[HoldingBreakdown]
    total_value: E | None = None
    total_cost_basis: E | None = None


class PortfolioResponse(BaseModel):
    accounts: list[AccountPortfolioBreakdown]
    total_value: E | None = None
    total_cost_basis: E | None = None
    currency_code: str = "EUR"


class PortfolioHistoryEntry(BaseModel):
    date: datetime
    total_value: E
    currency_code: str = "EUR"


class PortfolioHistoryResponse(BaseModel):
    items: list[PortfolioHistoryEntry]
    total: int
    limit: int
    offset: int


class NetWorthResponse(BaseModel):
    total_assets: E | None = None
    total_liabilities: E | None = None
    net_worth: E | None = None
    currency_code: str = "EUR"
    as_of: datetime | None = None
    accounts: list[AccountSummary] = Field(default_factory=list)


class NetWorthHistoryEntry(BaseModel):
    date: datetime
    net_worth: E
    total_assets: E
    total_liabilities: E
    currency_code: str = "EUR"


class NetWorthHistoryResponse(BaseModel):
    items: list[NetWorthHistoryEntry]
    total: int
    limit: int
    offset: int


class SyncRunResponse(BaseModel):
    id: str
    connector: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    items_processed: int | None = None
    error_message: str | None = None
    created_at: datetime | None = None


class SyncRunStatusCount(BaseModel):
    connector: str
    status: str
    count: int


class SyncRunListResponse(BaseModel):
    items: list[SyncRunResponse]
    status_counts: list[SyncRunStatusCount]
    total: int
    limit: int
    offset: int


# ── Service ───────────────────────────────────────────────────────────

_SORTABLE_ACCOUNT_FIELDS = {
    "name": Account.name,
    "account_type": Account.account_type,
    "current_balance": Account.current_balance,
    "created_at": Account.created_at,
    "updated_at": Account.updated_at,
}
_SORTABLE_TRANSACTION_FIELDS = {
    "occurred_at": Transaction.occurred_at,
    "amount": Transaction.amount,
    "created_at": Transaction.created_at,
}
_SORTABLE_SYNC_RUN_FIELDS = {
    "started_at": SyncRun.started_at,
    "completed_at": SyncRun.completed_at,
    "status": SyncRun.status,
    "connector": SyncRun.connector,
}


def _sort_field(
    mapping: dict[str, Any],
    sort_by: str,
    sort_order: str = "desc",
) -> Any:
    """Return the SQLAlchemy order_by expression from a field mapping."""
    col = mapping.get(sort_by)
    if col is None:
        col = next(iter(mapping.values()))  # default to first
        sort_order = "desc"
    return desc(col) if sort_order == "desc" else col.asc()


def _expr(*conditions: Any) -> Any:
    """Wrap conditions in ``and_()``, handling the empty case.

    SQLAlchemy 2.1+ deprecates calling ``and_()`` with no arguments;
    this helper transparently returns ``True`` (no-op) when there are
    no conditions so callers never need to check ``if conditions``.
    """
    if not conditions:
        return True  # no-op filter
    return and_(*conditions)


class ReadService:
    """Provides read-query methods for the read-only API.

    Each method returns Pydantic response models directly so that
    route handlers can return them verbatim.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Accounts ──────────────────────────────────────────────────────

    async def list_accounts(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "name",
        sort_order: str = "asc",
        account_type: str | None = None,
        is_active: bool | None = None,
    ) -> AccountDetailResponse:
        """List accounts for a tenant with optional filters."""
        conditions = [Account.tenant_id == tenant_id]  # type: ignore[attr-defined]

        if account_type is not None:
            conditions.append(Account.account_type == account_type)  # type: ignore[attr-defined]
        if is_active is not None:
            conditions.append(Account.is_active == is_active)  # type: ignore[attr-defined]

        # Count
        count_stmt = (
            select(func.count()).select_from(Account).where(_expr(*conditions))
        )
        total_result = await self._session.execute(count_stmt)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        # Fetch
        order = _sort_field(_SORTABLE_ACCOUNT_FIELDS, sort_by, sort_order)
        stmt = (
            select(Account)
            .where(_expr(*conditions))
            .order_by(order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[Account] = list(result.scalars().all())  # type: ignore[assignment]

        return AccountDetailResponse(
            items=[self._account_to_summary(a) for a in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_account(
        self, tenant_id: str, account_id: str
    ) -> AccountSummary | None:
        """Fetch a single account by ID (scoped to tenant)."""
        stmt = select(Account).where(
            Account.id == account_id,  # type: ignore[attr-defined]
            Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
        )
        result = await self._session.execute(stmt)
        account: Account | None = result.scalar_one_or_none()  # type: ignore[assignment]
        return self._account_to_summary(account) if account else None

    @staticmethod
    def _account_to_summary(a: Account) -> AccountSummary:
        return AccountSummary(
            id=str(a.id),
            name=a.name,
            account_type=str(a.account_type),
            account_subtype=a.account_subtype,
            currency_code=a.currency_code,
            current_balance=a.current_balance,
            available_balance=a.available_balance,
            provider_key=a.provider_key,
            is_active=a.is_active,
            created_at=a.created_at,
            updated_at=a.updated_at,
        )

    # ── Transactions ──────────────────────────────────────────────────

    async def list_account_transactions(
        self,
        tenant_id: str,
        account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "occurred_at",
        sort_order: str = "desc",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        transaction_type: str | None = None,
        security_id: str | None = None,
    ) -> TransactionListResponse:
        """List transactions for an account with optional filters."""
        conditions = [
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Transaction.account_id == account_id,  # type: ignore[attr-defined]
        ]

        if date_from is not None:
            conditions.append(Transaction.occurred_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Transaction.occurred_at <= date_to)  # type: ignore[attr-defined]
        if transaction_type is not None:
            conditions.append(  # type: ignore[attr-defined]
                Transaction.transaction_type == transaction_type
            )
        if security_id is not None:
            conditions.append(  # type: ignore[attr-defined]
                Transaction.security_id == security_id
            )

        # Count
        count_stmt = (
            select(func.count())
            .select_from(Transaction)
            .where(_expr(*conditions))
        )
        total_result = await self._session.execute(count_stmt)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        # Fetch
        order = _sort_field(_SORTABLE_TRANSACTION_FIELDS, sort_by, sort_order)
        stmt = (
            select(Transaction)
            .where(_expr(*conditions))
            .order_by(order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[Transaction] = list(result.scalars().all())  # type: ignore[assignment]

        return TransactionListResponse(
            items=[self._tx_to_response(t) for t in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _tx_to_response(t: Transaction) -> TransactionResponse:
        return TransactionResponse(
            id=str(t.id),
            account_id=str(t.account_id),
            security_id=str(t.security_id) if t.security_id else None,
            amount=t.amount,
            currency_code=t.currency_code,
            occurred_at=t.occurred_at,
            booked_at=t.booked_at,
            description=t.description,
            transaction_type=str(t.transaction_type),
            status=str(t.status),
            provider_key=t.provider_key,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )

    # ── Balances ──────────────────────────────────────────────────────

    async def list_account_balances(
        self,
        tenant_id: str,
        account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        balance_kind: str | None = None,
    ) -> BalanceListResponse:
        """List balance snapshots for an account."""
        conditions = [
            Balance.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Balance.account_id == account_id,  # type: ignore[attr-defined]
        ]

        if date_from is not None:
            conditions.append(Balance.observed_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Balance.observed_at <= date_to)  # type: ignore[attr-defined]
        if balance_kind is not None:
            conditions.append(Balance.balance_kind == balance_kind)  # type: ignore[attr-defined]

        # Count
        count_stmt = (
            select(func.count()).select_from(Balance).where(_expr(*conditions))
        )
        total_result = await self._session.execute(count_stmt)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        # Fetch (newest first)
        stmt = (
            select(Balance)
            .where(_expr(*conditions))
            .order_by(Balance.observed_at.desc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[Balance] = list(result.scalars().all())  # type: ignore[assignment]

        return BalanceListResponse(
            items=[
                BalanceResponse(
                    id=str(b.id),
                    account_id=str(b.account_id),
                    observed_at=b.observed_at,
                    balance_kind=str(b.balance_kind),
                    amount=b.amount,
                    currency_code=b.currency_code,
                    source=str(b.source),
                    created_at=b.created_at,
                )
                for b in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    # ── Portfolio ─────────────────────────────────────────────────────

    async def get_portfolio(self, tenant_id: str) -> PortfolioResponse:
        """Compute the current portfolio view.

        Gathers the latest holding snapshot per (account, security),
        enriches with the latest available price, and calculates
        unrealised P&L.  Returns a breakdown by account.
        """
        # Latest holding per (account_id, security_id) for this tenant
        # Uses a window/partition approach via a subquery
        latest_holding_subq = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_ts"),
            )
            .where(Holding.tenant_id == tenant_id)  # type: ignore[attr-defined]
            .group_by(Holding.account_id, Holding.security_id)  # type: ignore[attr-defined]
        ).subquery()

        holdings_q = (
            select(Holding)
            .join(
                latest_holding_subq,
                and_(
                    Holding.account_id == latest_holding_subq.c.account_id,  # type: ignore[attr-defined]
                    Holding.security_id == latest_holding_subq.c.security_id,  # type: ignore[attr-defined]
                    Holding.observed_at == latest_holding_subq.c.latest_ts,  # type: ignore[attr-defined]
                ),
            )
            .where(Holding.tenant_id == tenant_id)  # type: ignore[attr-defined]
            .order_by(Holding.account_id)  # type: ignore[attr-defined]
        )
        result = await self._session.execute(holdings_q)
        holdings: list[Holding] = list(result.scalars().all())  # type: ignore[assignment]

        if not holdings:
            return PortfolioResponse(
                accounts=[], total_value=E("0"), total_cost_basis=E("0")
            )

        # Resolve unique security IDs
        security_ids = list({h.security_id for h in holdings})
        account_ids = list({h.account_id for h in holdings})

        # Fetch account details
        acct_result = await self._session.execute(
            select(Account).where(
                Account.id.in_(account_ids),  # type: ignore[attr-defined]
                Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
            )
        )
        account_map: dict[str, Account] = {
            str(a.id): a
            for a in acct_result.scalars().all()  # type: ignore[assignment]
        }

        # Fetch securities
        sec_result = await self._session.execute(
            select(Security).where(Security.id.in_(security_ids))  # type: ignore[attr-defined]
        )
        sec_map: dict[str, Security] = {
            str(s.id): s
            for s in sec_result.scalars().all()  # type: ignore[assignment]
        }

        # Fetch latest prices per security
        price_map: dict[str, SecurityPrice] = {}
        for sid in security_ids:
            price_result = await self._session.execute(
                select(SecurityPrice)
                .where(
                    SecurityPrice.security_id == sid,  # type: ignore[attr-defined]
                    SecurityPrice.interval == "1d",  # type: ignore[attr-defined]
                )
                .order_by(SecurityPrice.timestamp.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
            sp: SecurityPrice | None = price_result.scalar_one_or_none()  # type: ignore[assignment]
            if sp is not None:
                price_map[sid] = sp

        # Build account breakdowns
        by_account: dict[str, list[Holding]] = {}
        for h in holdings:
            by_account.setdefault(str(h.account_id), []).append(h)

        accounts_breakdown: list[AccountPortfolioBreakdown] = []
        total_value = E("0")
        total_cost_basis = E("0")

        for acct_id, acct_holdings in by_account.items():
            acct = account_map.get(acct_id)
            holding_breakdowns: list[HoldingBreakdown] = []
            acct_value = E("0")
            acct_cost = E("0")

            for h in acct_holdings:
                sec = sec_map.get(str(h.security_id))
                latest = price_map.get(str(h.security_id))

                price = h.price
                if price is None and latest is not None:
                    price = latest.price_close

                quantity = h.quantity
                market_value = h.market_value
                if market_value is None and price is not None:
                    market_value = quantity * price

                cost_basis = h.cost_basis
                unrealised_pl: E | None = None
                unrealised_pl_pct: E | None = None
                if cost_basis is not None and market_value is not None:
                    unrealised_pl = market_value - cost_basis
                    if cost_basis != E("0"):
                        pct = (unrealised_pl / cost_basis) * E("100")
                        unrealised_pl_pct = pct

                if market_value is not None:
                    acct_value += market_value
                if cost_basis is not None:
                    acct_cost += cost_basis

                holding_breakdowns.append(
                    HoldingBreakdown(
                        security_id=str(h.security_id),
                        ticker=sec.ticker if sec else None,
                        security_name=sec.name if sec else "Unknown",
                        security_type=(
                            str(sec.security_type) if sec else "other"
                        ),
                        quantity=quantity,
                        cost_basis=cost_basis,
                        cost_basis_currency=h.cost_basis_currency,
                        market_value=market_value,
                        price=price,
                        price_currency=h.price_currency
                        or (latest.currency_code if latest else None),
                        currency_code=h.currency_code,
                        unrealised_pl=unrealised_pl,
                        unrealised_pl_pct=unrealised_pl_pct,
                    )
                )

            total_value += acct_value
            total_cost_basis += acct_cost

            accounts_breakdown.append(
                AccountPortfolioBreakdown(
                    account_id=acct_id,
                    account_name=acct.name if acct else acct_id,
                    account_type=str(acct.account_type) if acct else "unknown",
                    holdings=holding_breakdowns,
                    total_value=acct_value,
                    total_cost_basis=acct_cost,
                )
            )

        return PortfolioResponse(
            accounts=accounts_breakdown,
            total_value=total_value,
            total_cost_basis=total_cost_basis,
        )

    async def get_portfolio_history(
        self,
        tenant_id: str,
        *,
        limit: int = 90,
        offset: int = 0,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> PortfolioHistoryResponse:
        """Compute portfolio value over time.

        Uses the sum of (holding market_value) on each observed date
        across all holdings for the tenant.  This gives a daily view
        of total investment portfolio value.
        """
        conditions = [Holding.tenant_id == tenant_id]  # type: ignore[attr-defined]

        if date_from is not None:
            conditions.append(Holding.observed_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Holding.observed_at <= date_to)  # type: ignore[attr-defined]

        # Aggregate: sum(market_value) grouped by date(observed_at)
        date_col = func.date_trunc("day", Holding.observed_at)  # type: ignore[attr-defined]
        agg_q = (
            select(
                date_col.label("date"),
                func.sum(Holding.market_value).label("total_value"),  # type: ignore[attr-defined]
            )
            .where(_expr(*conditions))
            .group_by(date_col)
            .order_by(desc(date_col))
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(agg_q)
        rows = result.all()

        items = [
            PortfolioHistoryEntry(
                date=row.date,
                total_value=row.total_value or E("0"),
            )
            for row in rows
        ]

        # Total count
        count_q = (
            select(func.count(func.distinct(date_col)))
            .select_from(Holding)
            .where(_expr(*conditions))
        )
        count_result = await self._session.execute(count_q)
        total: int = count_result.scalar() or 0  # type: ignore[assignment]

        return PortfolioHistoryResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    # ── Securities ────────────────────────────────────────────────────

    async def list_securities(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        security_type: str | None = None,
        search: str | None = None,
    ) -> SecurityListResponse:
        """List known securities with optional filtering and search."""
        conditions: list[Any] = []

        if security_type is not None:
            conditions.append(Security.security_type == security_type)  # type: ignore[attr-defined]
        if search is not None:
            pattern = f"%{search}%"
            conditions.append(
                or_(
                    Security.name.ilike(pattern),  # type: ignore[attr-defined]
                    Security.ticker.ilike(pattern),  # type: ignore[attr-defined]
                    Security.isin.ilike(pattern),  # type: ignore[attr-defined]
                    Security.figi.ilike(pattern),  # type: ignore[attr-defined]
                )
            )

        # Count
        count_stmt = (
            select(func.count()).select_from(Security).where(_expr(*conditions))
        )
        total_result = await self._session.execute(count_stmt)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        # Fetch
        stmt = (
            select(Security)
            .where(_expr(*conditions))
            .order_by(Security.name.asc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[Security] = list(result.scalars().all())  # type: ignore[assignment]

        # Get latest price for each security
        items: list[SecurityInfo] = []
        for s in rows:
            latest_price = await self._session.execute(
                select(SecurityPrice)
                .where(
                    SecurityPrice.security_id == s.id,  # type: ignore[attr-defined]
                    SecurityPrice.interval == "1d",  # type: ignore[attr-defined]
                )
                .order_by(SecurityPrice.timestamp.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
            sp: SecurityPrice | None = latest_price.scalar_one_or_none()  # type: ignore[assignment]

            items.append(
                SecurityInfo(
                    id=str(s.id),
                    isin=s.isin,
                    figi=s.figi,
                    ticker=s.ticker,
                    name=s.name,
                    security_type=str(s.security_type),
                    currency_code=s.currency_code,
                    latest_price=sp.price_close if sp else None,
                    latest_price_currency=sp.currency_code if sp else None,
                    latest_price_timestamp=sp.timestamp if sp else None,
                    created_at=s.created_at,
                    updated_at=s.updated_at,
                )
            )

        return SecurityListResponse(
            items=items, total=total, limit=limit, offset=offset
        )

    async def get_security_prices(
        self,
        security_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        interval: str = "1d",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> SecurityPriceListResponse:
        """List price observations for a security."""
        conditions: list[Any] = [
            SecurityPrice.security_id == security_id,  # type: ignore[attr-defined]
            SecurityPrice.interval == interval,  # type: ignore[attr-defined]
        ]

        if date_from is not None:
            conditions.append(SecurityPrice.timestamp >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(SecurityPrice.timestamp <= date_to)  # type: ignore[attr-defined]

        # Count
        count_stmt = (
            select(func.count())
            .select_from(SecurityPrice)
            .where(_expr(*conditions))
        )
        total_result = await self._session.execute(count_stmt)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        # Fetch
        stmt = (
            select(SecurityPrice)
            .where(_expr(*conditions))
            .order_by(SecurityPrice.timestamp.desc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[SecurityPrice] = list(result.scalars().all())  # type: ignore[assignment]

        return SecurityPriceListResponse(
            items=[
                SecurityPriceResponse(
                    id=str(sp.id),
                    security_id=str(sp.security_id),
                    timestamp=sp.timestamp,
                    price_open=sp.price_open,
                    price_high=sp.price_high,
                    price_low=sp.price_low,
                    price_close=sp.price_close,
                    volume=sp.volume,
                    source=sp.source,
                    interval=sp.interval,
                    currency_code=sp.currency_code,
                )
                for sp in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    # ── Net Worth ─────────────────────────────────────────────────────

    async def get_net_worth(self, tenant_id: str) -> NetWorthResponse:
        """Aggregate net worth across all accounts.

        Computes total assets (credit balances) and liabilities
        (debit balances) grouped by currency.  Uses the latest
        current_balance on each account.
        """
        result = await self._session.execute(
            select(Account).where(
                Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
                Account.is_active == True,  # noqa: E712
            )
        )
        accounts: list[Account] = list(result.scalars().all())  # type: ignore[assignment]

        total_assets = E("0")
        total_liabilities = E("0")
        account_summaries: list[AccountSummary] = []
        now = datetime.now(UTC)

        for a in accounts:
            bal = a.current_balance
            if bal is not None:
                if bal >= E("0"):
                    total_assets += bal
                else:
                    total_liabilities += abs(bal)
            account_summaries.append(self._account_to_summary(a))

        net_worth = total_assets - total_liabilities

        return NetWorthResponse(
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            net_worth=net_worth,
            as_of=now,
            accounts=account_summaries,
        )

    async def get_net_worth_history(
        self,
        tenant_id: str,
        *,
        limit: int = 90,
        offset: int = 0,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> NetWorthHistoryResponse:
        """Net worth time series using balance snapshots.

        Sums booked/available balance amounts grouped by date across
        all accounts for the tenant.  Provides a per-date view of
        total assets, liabilities, and net worth.
        """
        conditions: list[Any] = [
            Balance.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Balance.balance_kind.in_(["booked", "available"]),  # type: ignore[attr-defined]
        ]

        if date_from is not None:
            conditions.append(Balance.observed_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Balance.observed_at <= date_to)  # type: ignore[attr-defined]

        date_col = func.date_trunc("day", Balance.observed_at)  # type: ignore[attr-defined]

        # Aggregate: sum(amount) grouped by date
        agg_q = (
            select(
                date_col.label("date"),
                func.sum(Balance.amount).label("net_amount"),  # type: ignore[attr-defined]
            )
            .where(_expr(*conditions))
            .group_by(date_col)
            .order_by(desc(date_col))
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(agg_q)
        rows = result.all()

        items = [
            NetWorthHistoryEntry(
                date=row.date,
                net_worth=row.net_amount or E("0"),
                total_assets=(
                    row.net_amount
                    if (row.net_amount or E("0")) >= E("0")
                    else E("0")
                ),
                total_liabilities=(
                    abs(row.net_amount)
                    if (row.net_amount or E("0")) < E("0")
                    else E("0")
                ),
            )
            for row in rows
        ]

        count_q = (
            select(func.count(func.distinct(date_col)))
            .select_from(Balance)
            .where(_expr(*conditions))
        )
        count_result = await self._session.execute(count_q)
        total: int = count_result.scalar() or 0  # type: ignore[assignment]

        return NetWorthHistoryResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    # ── Sync Runs ─────────────────────────────────────────────────────

    async def list_sync_runs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        connector: str | None = None,
        status: str | None = None,
        sort_by: str = "started_at",
        sort_order: str = "desc",
    ) -> SyncRunListResponse:
        """List sync run history with status counts per connector."""
        conditions: list[Any] = []

        if connector is not None:
            conditions.append(SyncRun.connector == connector)  # type: ignore[attr-defined]
        if status is not None:
            conditions.append(SyncRun.status == status)  # type: ignore[attr-defined]

        # Status counts per connector
        count_by_q = (
            select(
                SyncRun.connector,
                SyncRun.status,
                func.count().label("cnt"),
            )
            .where(_expr(*conditions))
            .group_by(SyncRun.connector, SyncRun.status)
        )
        count_result = await self._session.execute(count_by_q)
        status_counts = [
            SyncRunStatusCount(
                connector=str(row.connector),
                status=str(row.status),
                count=int(row.cnt),
            )
            for row in count_result
        ]

        # Total items matching filters
        total_query = (
            select(func.count()).select_from(SyncRun).where(_expr(*conditions))
        )
        total_result = await self._session.execute(total_query)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        # Fetch items
        order = _sort_field(_SORTABLE_SYNC_RUN_FIELDS, sort_by, sort_order)
        stmt = (
            select(SyncRun)
            .where(_expr(*conditions))
            .order_by(order)
            .offset(offset)
            .limit(limit)
        )
        items_result = await self._session.execute(stmt)
        rows: list[SyncRun] = list(items_result.scalars().all())  # type: ignore[assignment]

        return SyncRunListResponse(
            items=[
                SyncRunResponse(
                    id=str(sr.id),
                    connector=sr.connector,
                    status=str(sr.status),
                    started_at=sr.started_at,
                    completed_at=sr.completed_at,
                    items_processed=sr.items_processed,
                    error_message=sr.error_message,
                    created_at=sr.created_at,
                )
                for sr in rows
            ],
            status_counts=status_counts,
            total=total,
            limit=limit,
            offset=offset,
        )
