"""Operational read queries not yet split into domain components."""

from __future__ import annotations

from datetime import (
    datetime,  # noqa: TC003 — needed by runtime query annotations
)
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, cast, desc, func, select

from finance_sync.models.balance import Balance
from finance_sync.models.card_transaction import CardTransaction
from finance_sync.models.credential import Credential
from finance_sync.models.enums import TransactionType
from finance_sync.models.holding import Holding
from finance_sync.models.scheduled_payment import ScheduledPayment
from finance_sync.models.sync_run import SyncRun
from finance_sync.models.transaction import Transaction
from finance_sync.schemas.freshness import (
    CollectionMeta,
    CoverageInfo,
    build_meta,
    freshness_for,
)
from finance_sync.services.read.pagination import expression as _expr
from finance_sync.services.read.pagination import sort_field as _sort_field
from finance_sync.services.read.schemas import (
    BalanceListResponse,
    BalanceResponse,
    CardTransactionListResponse,
    CardTransactionResponse,
    CashflowHistoryEntry,
    CashflowHistoryResponse,
    DividendListResponse,
    NetWorthHistoryEntry,
    NetWorthHistoryResponse,
    PortfolioHistoryEntry,
    PortfolioHistoryResponse,
    ScheduledPaymentListResponse,
    ScheduledPaymentResponse,
    SyncRunListResponse,
    SyncRunResponse,
    SyncRunStatusCount,
    TopLevelTransactionListResponse,
    TransactionResponse,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.services.visibility import ReadScope

E = Decimal

_SORTABLE_TRANSACTION_FIELDS = {
    "occurred_at": Transaction.occurred_at,
    "amount": Transaction.amount,
    "created_at": Transaction.created_at,
}
_SORTABLE_SCHEDULED_PAYMENT_FIELDS = {
    "next_execution_date": ScheduledPayment.next_execution_date,
    "amount": ScheduledPayment.amount,
    "created_at": ScheduledPayment.created_at,
}
_SORTABLE_CARD_TRANSACTION_FIELDS = {
    "occurred_at": CardTransaction.occurred_at,
    "amount": CardTransaction.amount,
    "created_at": CardTransaction.created_at,
}
_SORTABLE_SYNC_RUN_FIELDS = {
    "started_at": SyncRun.started_at,
    "completed_at": SyncRun.completed_at,
    "status": SyncRun.status,
    "connector": SyncRun.connector,
}


class OperationalReadService:
    """Facade component for remaining operational read queries."""

    def __init__(
        self, session: AsyncSession, *, scope: ReadScope | None = None
    ) -> None:
        self._session = session
        self._scope = scope

    def _account_scope_condition(self) -> Any:
        """Return the Account predicate when a scope is set (else True)."""
        if self._scope is None:
            return True
        return self._scope.account_filter()

    def _derived_scope_condition(self, model: Any) -> Any:
        """Return ``model.account_id IN (visible ids)`` when scoped."""
        if self._scope is None:
            return True
        return model.account_id.in_(  # type: ignore[attr-defined]
            self._scope.account_ids_subquery()
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
            transaction_type=TransactionType(str(t.transaction_type)),
            status=str(t.status),
            provider_key=t.provider_key,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )

    async def list_transactions(
        self,
        tenant_id: str,
        *,
        account_id: str | None = None,
        provider_key: str | None = None,
        status: str | None = None,
        transaction_type: str | None = None,
        currency_code: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "occurred_at",
        sort_order: str = "desc",
    ) -> TopLevelTransactionListResponse:
        """List transactions across all of a tenant's accounts.

        Supports the documented ``GET /transactions`` filters
        (accountId, provider, status, type, from, to, currency).
        """
        conditions: list[Any] = [
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            self._derived_scope_condition(Transaction),
        ]

        if account_id is not None:
            conditions.append(Transaction.account_id == account_id)  # type: ignore[attr-defined]
        if provider_key is not None:
            conditions.append(  # type: ignore[attr-defined]
                Transaction.provider_key == provider_key
            )
        if status is not None:
            conditions.append(Transaction.status == status)  # type: ignore[attr-defined]
        if transaction_type is not None:
            conditions.append(  # type: ignore[attr-defined]
                Transaction.transaction_type == transaction_type
            )
        if currency_code is not None:
            conditions.append(  # type: ignore[attr-defined]
                Transaction.currency_code == currency_code
            )
        if date_from is not None:
            conditions.append(Transaction.occurred_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Transaction.occurred_at <= date_to)  # type: ignore[attr-defined]

        # Count + latest observation in a single query
        meta_row = (
            await self._session.execute(
                select(
                    func.count().label("total"),
                    func.max(Transaction.occurred_at).label("as_of"),  # type: ignore[attr-defined]
                )
                .select_from(Transaction)
                .where(_expr(*conditions))
            )
        ).one()
        total: int = meta_row.total or 0  # type: ignore[assignment]

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

        return TopLevelTransactionListResponse(
            items=[self._tx_to_response(t) for t in rows],
            total=total,
            limit=limit,
            offset=offset,
            meta=CollectionMeta(
                as_of=meta_row.as_of,
                freshness=freshness_for(meta_row.as_of),
            ),
        )

    async def list_dividends(
        self,
        tenant_id: str,
        *,
        account_id: str | None = None,
        security_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> DividendListResponse:
        """List dividend-type transactions across a tenant's accounts."""
        conditions: list[Any] = [
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Transaction.transaction_type == TransactionType.DIVIDEND,  # type: ignore[attr-defined]
            self._derived_scope_condition(Transaction),
        ]

        if account_id is not None:
            conditions.append(Transaction.account_id == account_id)  # type: ignore[attr-defined]
        if security_id is not None:
            conditions.append(  # type: ignore[attr-defined]
                Transaction.security_id == security_id
            )
        if date_from is not None:
            conditions.append(Transaction.occurred_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Transaction.occurred_at <= date_to)  # type: ignore[attr-defined]

        meta_row = (
            await self._session.execute(
                select(
                    func.count().label("total"),
                    func.max(Transaction.occurred_at).label("as_of"),  # type: ignore[attr-defined]
                )
                .select_from(Transaction)
                .where(_expr(*conditions))
            )
        ).one()
        total: int = meta_row.total or 0  # type: ignore[assignment]

        order = _sort_field(_SORTABLE_TRANSACTION_FIELDS, "occurred_at", "desc")
        stmt = (
            select(Transaction)
            .where(_expr(*conditions))
            .order_by(order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[Transaction] = list(result.scalars().all())  # type: ignore[assignment]

        return DividendListResponse(
            items=[self._tx_to_response(t) for t in rows],
            total=total,
            limit=limit,
            offset=offset,
            meta=CollectionMeta(
                as_of=meta_row.as_of,
                freshness=freshness_for(meta_row.as_of),
            ),
        )

    async def list_scheduled_payments(
        self,
        tenant_id: str,
        *,
        account_id: str | None = None,
        provider_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "next_execution_date",
        sort_order: str = "desc",
    ) -> ScheduledPaymentListResponse:
        """List scheduled payments with optional account/provider filters."""
        conditions = [
            ScheduledPayment.tenant_id == tenant_id,  # type: ignore[attr-defined]
            self._derived_scope_condition(ScheduledPayment),
        ]

        if account_id is not None:
            conditions.append(  # type: ignore[attr-defined]
                ScheduledPayment.account_id == account_id
            )
        if provider_key is not None:
            conditions.append(  # type: ignore[attr-defined]
                ScheduledPayment.provider_key == provider_key
            )

        count_stmt = (
            select(func.count())
            .select_from(ScheduledPayment)
            .where(_expr(*conditions))
        )
        total_result = await self._session.execute(count_stmt)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        order = _sort_field(
            _SORTABLE_SCHEDULED_PAYMENT_FIELDS, sort_by, sort_order
        )
        stmt = (
            select(ScheduledPayment)
            .where(_expr(*conditions))
            .order_by(order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[ScheduledPayment] = list(result.scalars().all())  # type: ignore[assignment]

        return ScheduledPaymentListResponse(
            items=[self._schedule_to_response(s) for s in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _schedule_to_response(s: ScheduledPayment) -> ScheduledPaymentResponse:
        return ScheduledPaymentResponse(
            id=str(s.id),
            account_id=str(s.account_id) if s.account_id else None,
            provider_key=s.provider_key,
            external_schedule_id=s.external_schedule_id,
            amount=s.amount,
            currency_code=s.currency_code,
            amount_in_base=s.amount_in_base,
            frequency=str(s.frequency),
            interval=s.interval,
            next_execution_date=s.next_execution_date,
            end_date=s.end_date,
            max_executions=s.max_executions,
            execution_count=s.execution_count or 0,
            counterparty_name=s.counterparty_name,
            counterparty_iban=s.counterparty_iban,
            description=s.description,
            status=str(s.status),
            created_at=s.created_at,
            updated_at=s.updated_at,
        )

    async def list_card_transactions(
        self,
        tenant_id: str,
        *,
        account_id: str | None = None,
        provider_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "occurred_at",
        sort_order: str = "desc",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> CardTransactionListResponse:
        """List card transactions with optional account/provider filters."""
        conditions = [
            CardTransaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            self._derived_scope_condition(CardTransaction),
        ]

        if account_id is not None:
            conditions.append(  # type: ignore[attr-defined]
                CardTransaction.account_id == account_id
            )
        if provider_key is not None:
            conditions.append(  # type: ignore[attr-defined]
                CardTransaction.provider_key == provider_key
            )
        if date_from is not None:
            conditions.append(CardTransaction.occurred_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(CardTransaction.occurred_at <= date_to)  # type: ignore[attr-defined]

        count_stmt = (
            select(func.count())
            .select_from(CardTransaction)
            .where(_expr(*conditions))
        )
        total_result = await self._session.execute(count_stmt)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        order = _sort_field(
            _SORTABLE_CARD_TRANSACTION_FIELDS, sort_by, sort_order
        )
        stmt = (
            select(CardTransaction)
            .where(_expr(*conditions))
            .order_by(order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows: list[CardTransaction] = list(result.scalars().all())  # type: ignore[assignment]

        return CardTransactionListResponse(
            items=[self._card_tx_to_response(t) for t in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _card_tx_to_response(t: CardTransaction) -> CardTransactionResponse:
        return CardTransactionResponse(
            id=str(t.id),
            account_id=str(t.account_id) if t.account_id else None,
            provider_key=t.provider_key,
            external_card_transaction_id=t.external_card_transaction_id,
            amount=t.amount,
            currency_code=t.currency_code,
            amount_in_base=t.amount_in_base,
            merchant_name=t.merchant_name,
            merchant_city=t.merchant_city,
            merchant_country=t.merchant_country,
            mcc=t.mcc,
            card_id=t.card_id,
            card_type=t.card_type,
            card_last_four=t.card_last_four,
            occurred_at=t.occurred_at,
            booked_at=t.booked_at,
            transaction_type=str(t.transaction_type),
            authorization_type=str(t.authorization_type),
            description=t.description,
            status=str(t.status),
            created_at=t.created_at,
            updated_at=t.updated_at,
        )

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
            self._derived_scope_condition(Balance),
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
        conditions = [
            Holding.tenant_id == tenant_id,  # type: ignore[attr-defined]
            self._derived_scope_condition(Holding),
        ]

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
            self._derived_scope_condition(Balance),
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

    async def get_cashflow_history(
        self,
        tenant_id: str,
        *,
        limit: int = 90,
        offset: int = 0,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_id: str | None = None,
    ) -> CashflowHistoryResponse:
        """Return cash flow time series (daily buckets).

        Uses booked transactions, aggregated by day.
        """
        conditions: list[Any] = [
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Transaction.status == "booked",  # type: ignore[attr-defined]
            self._derived_scope_condition(Transaction),
        ]

        if date_from is not None:
            conditions.append(Transaction.occurred_at >= date_from)  # type: ignore[attr-defined]
        if date_to is not None:
            conditions.append(Transaction.occurred_at <= date_to)  # type: ignore[attr-defined]
        if account_id is not None:
            conditions.append(Transaction.account_id == account_id)  # type: ignore[attr-defined]

        date_col = func.date_trunc("day", Transaction.occurred_at)  # type: ignore[attr-defined]

        inflow_expr = func.coalesce(
            func.sum(Transaction.amount).filter(
                Transaction.amount > 0  # type: ignore[attr-defined]
            ),
            E("0"),
        ).label("inflows")

        outflow_expr = func.coalesce(
            func.sum(-Transaction.amount).filter(
                Transaction.amount < 0  # type: ignore[attr-defined]
            ),
            E("0"),
        ).label("outflows")

        agg_q = (
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
        result = await self._session.execute(agg_q)
        rows = result.all()

        items = [
            CashflowHistoryEntry(
                date=row.date,
                inflows=row.inflows or E("0"),
                outflows=row.outflows or E("0"),
                net=row.net or E("0"),
                transaction_count=row.transaction_count or 0,
            )
            for row in rows
        ]

        # Total count of distinct days
        count_q = (
            select(func.count(func.distinct(date_col)))
            .select_from(Transaction)
            .where(_expr(*conditions))
        )
        count_result = await self._session.execute(count_q)
        total: int = count_result.scalar() or 0  # type: ignore[assignment]

        # Get actual period bounds from data
        period_start: datetime | None = None
        period_end: datetime | None = None
        if items:
            period_start = min(i.date for i in items)
            period_end = max(i.date for i in items)

        # Account coverage for the same conditions
        acct_count_q = (
            select(func.count(func.distinct(Transaction.account_id)))  # type: ignore[attr-defined]
            .select_from(Transaction)
            .where(_expr(*conditions))
        )
        acct_result = await self._session.execute(acct_count_q)
        account_count: int = acct_result.scalar() or 0  # type: ignore[assignment]

        return CashflowHistoryResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            period_start=period_start,
            period_end=period_end,
            meta=build_meta(
                as_of=period_end,
                coverage=CoverageInfo(
                    accounts=account_count,
                    items=total,
                ),
            ),
        )

    async def list_sync_runs(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        connector: str | None = None,
        status: str | None = None,
        sort_by: str = "started_at",
        sort_order: str = "desc",
    ) -> SyncRunListResponse:
        """List sync run history with status counts per connector."""
        conditions: list[Any] = []

        if tenant_id is not None:
            conditions.append(Credential.tenant_id == tenant_id)

        if connector is not None:
            conditions.append(SyncRun.connector == connector)  # type: ignore[attr-defined]
        if status is not None:
            conditions.append(SyncRun.status == status)  # type: ignore[attr-defined]

        # Status counts per connector
        count_by_q = select(
            SyncRun.connector,
            SyncRun.status,
            func.count().label("cnt"),
        ).select_from(SyncRun)
        if tenant_id is not None:
            count_by_q = count_by_q.join(
                Credential, cast(Credential.id, String) == SyncRun.connection_id
            )
        count_by_q = count_by_q.where(_expr(*conditions)).group_by(
            SyncRun.connector, SyncRun.status
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
        total_query = select(func.count()).select_from(SyncRun)
        if tenant_id is not None:
            total_query = total_query.join(
                Credential, cast(Credential.id, String) == SyncRun.connection_id
            )
        total_query = total_query.where(_expr(*conditions))
        total_result = await self._session.execute(total_query)
        total: int = total_result.scalar() or 0  # type: ignore[assignment]

        # Fetch items
        order = _sort_field(_SORTABLE_SYNC_RUN_FIELDS, sort_by, sort_order)
        stmt = select(SyncRun)
        if tenant_id is not None:
            stmt = stmt.join(
                Credential, cast(Credential.id, String) == SyncRun.connection_id
            )
        stmt = (
            stmt.where(_expr(*conditions))
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
                    cursor=sr.cursor,
                    items_processed=sr.items_processed,
                    error_message=sr.error_message,
                    error_category=sr.error_category,
                    warnings=list(sr.warnings or []),
                    duration_seconds=(
                        (sr.completed_at - sr.started_at).total_seconds()
                        if sr.completed_at
                        else None
                    ),
                    created_at=sr.created_at,
                )
                for sr in rows
            ],
            status_counts=status_counts,
            total=total,
            limit=limit,
            offset=offset,
        )
