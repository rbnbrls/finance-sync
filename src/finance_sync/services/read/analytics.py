"""Analytics read component for account aggregates and cashflow."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from finance_sync.models.account import Account
from finance_sync.models.transaction import Transaction
from finance_sync.schemas.freshness import CoverageInfo, build_meta

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.services.visibility import ReadScope

E = Decimal


class AnalyticsReadService:
    """Read net-worth and cashflow aggregates for one session and scope."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        scope: ReadScope | None = None,
    ) -> None:
        self._session = session
        self._scope = scope

    def _account_condition(self) -> Any:
        return self._scope.account_filter() if self._scope else True

    def _derived_condition(self, model: Any) -> Any:
        if self._scope is None:
            return True
        return model.account_id.in_(self._scope.account_ids_subquery())

    async def get_net_worth(self, tenant_id: str) -> Any:
        from finance_sync.services.read_api import (
            AccountSummary,
            NetWorthResponse,
        )

        result = await self._session.execute(
            select(Account).where(
                Account.tenant_id == tenant_id,
                Account.is_active,
                self._account_condition(),
            )
        )
        accounts: list[Account] = list(result.scalars().all())
        total_assets = E("0")
        total_liabilities = E("0")
        summaries: list[Any] = []
        for account in accounts:
            # Investment accounts expose the positions NAV explicitly.  Keep
            # current_balance as cash, but use NAV for net-worth aggregation
            # so splitting the fields does not change the reported assets.
            balance = (
                account.net_asset_value
                if account.net_asset_value is not None
                else account.current_balance
            )
            if balance is not None:
                if balance >= E("0"):
                    total_assets += balance
                else:
                    total_liabilities += abs(balance)
            summaries.append(
                AccountSummary(
                    id=str(account.id),
                    connection_id=(
                        str(account.connection_id)
                        if account.connection_id
                        else None
                    ),
                    name=account.name,
                    account_type=str(account.account_type),
                    account_subtype=account.account_subtype,
                    currency_code=account.currency_code,
                    current_balance=account.current_balance,
                    available_balance=account.available_balance,
                    net_asset_value=account.net_asset_value,
                    provider_key=account.provider_key,
                    is_active=account.is_active,
                    owner_user_id=account.owner_user_id,
                    created_at=account.created_at,
                    updated_at=account.updated_at,
                )
            )
        return NetWorthResponse(
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            net_worth=total_assets - total_liabilities,
            as_of=datetime.now(UTC),
            accounts=summaries,
        )

    async def get_cashflow(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_id: str | None = None,
    ) -> Any:
        from finance_sync.services.read_api import CashflowResponse

        conditions: list[Any] = [
            Transaction.tenant_id == tenant_id,
            Transaction.status == "booked",
            self._derived_condition(Transaction),
        ]
        if date_from is not None:
            conditions.append(Transaction.occurred_at >= date_from)
        if date_to is not None:
            conditions.append(Transaction.occurred_at <= date_to)
        if account_id is not None:
            conditions.append(Transaction.account_id == account_id)
        inflows = func.coalesce(
            func.sum(Transaction.amount).filter(Transaction.amount > 0),
            E("0"),
        ).label("total_inflows")
        outflows = func.coalesce(
            func.sum(-Transaction.amount).filter(Transaction.amount < 0),
            E("0"),
        ).label("total_outflows")
        result = await self._session.execute(
            select(
                inflows,
                outflows,
                func.count().label("transaction_count"),
                func.min(Transaction.occurred_at).label("period_start"),
                func.max(Transaction.occurred_at).label("period_end"),
            )
            .select_from(Transaction)
            .where(*conditions)
        )
        row = result.one()
        account_result = await self._session.execute(
            select(func.count(func.distinct(Transaction.account_id)))
            .select_from(Transaction)
            .where(*conditions)
        )
        account_count: int = account_result.scalar() or 0
        total_inflows = row.total_inflows or E("0")
        total_outflows = row.total_outflows or E("0")
        return CashflowResponse(
            total_inflows=total_inflows,
            total_outflows=total_outflows,
            net_cashflow=total_inflows - total_outflows,
            transaction_count=row.transaction_count or 0,
            period_start=row.period_start,
            period_end=row.period_end,
            meta=build_meta(
                as_of=row.period_end,
                coverage=CoverageInfo(
                    accounts=account_count,
                    items=row.transaction_count or 0,
                ),
            ),
        )
