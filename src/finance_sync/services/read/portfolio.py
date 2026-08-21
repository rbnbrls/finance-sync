"""Portfolio and holdings read component."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select

from finance_sync.models.account import Account
from finance_sync.models.holding import Holding
from finance_sync.models.security import Security
from finance_sync.schemas.freshness import CollectionMeta, freshness_for
from finance_sync.services.read.pagination import expression
from finance_sync.services.read.prices import fetch_latest_daily_prices

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.services.visibility import ReadScope

E = Decimal


class PortfolioReadService:
    """Read portfolio and current holdings for one session and scope."""

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

    async def get_portfolio(self, tenant_id: str) -> Any:
        """Compute current portfolio value and per-account breakdown."""
        from finance_sync.services.read_api import (
            AccountPortfolioBreakdown,
            HoldingBreakdown,
            PortfolioResponse,
        )

        latest_holding_subq = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_ts"),
            )
            .where(
                Holding.tenant_id == tenant_id,
                self._derived_condition(Holding),
            )
            .group_by(Holding.account_id, Holding.security_id)
        ).subquery()
        holdings_q = (
            select(Holding)
            .join(
                latest_holding_subq,
                and_(
                    Holding.account_id == latest_holding_subq.c.account_id,
                    Holding.security_id == latest_holding_subq.c.security_id,
                    Holding.observed_at == latest_holding_subq.c.latest_ts,
                ),
            )
            .where(
                Holding.tenant_id == tenant_id,
                self._derived_condition(Holding),
            )
            .order_by(Holding.account_id)
        )
        result = await self._session.execute(holdings_q)
        holdings: list[Holding] = list(result.scalars().all())
        if not holdings:
            return PortfolioResponse(
                accounts=[], total_value=E("0"), total_cost_basis=E("0")
            )

        security_ids = list({h.security_id for h in holdings})
        account_ids = list({h.account_id for h in holdings})
        acct_result = await self._session.execute(
            select(Account).where(
                Account.id.in_(account_ids),
                Account.tenant_id == tenant_id,
                self._account_condition(),
            )
        )
        account_map: dict[str, Account] = {
            str(a.id): a for a in acct_result.scalars().all()
        }
        sec_result = await self._session.execute(
            select(Security).where(Security.id.in_(security_ids))
        )
        sec_map: dict[str, Security] = {
            str(s.id): s for s in sec_result.scalars().all()
        }
        price_map = await fetch_latest_daily_prices(self._session, security_ids)

        by_account: dict[str, list[Holding]] = {}
        for holding in holdings:
            by_account.setdefault(str(holding.account_id), []).append(holding)

        accounts: list[Any] = []
        total_value = E("0")
        total_cost_basis = E("0")
        for account_id, account_holdings in by_account.items():
            account = account_map.get(account_id)
            breakdowns: list[Any] = []
            account_value = E("0")
            account_cost = E("0")
            for holding in account_holdings:
                security = sec_map.get(str(holding.security_id))
                latest = price_map.get(str(holding.security_id))
                price = holding.price or (
                    latest.price_close if latest is not None else None
                )
                market_value = holding.market_value
                if market_value is None and price is not None:
                    market_value = holding.quantity * price
                cost_basis = holding.cost_basis
                unrealised_pl = None
                unrealised_pl_pct = None
                if cost_basis is not None and market_value is not None:
                    unrealised_pl = market_value - cost_basis
                    if cost_basis != E("0"):
                        unrealised_pl_pct = (
                            unrealised_pl / cost_basis
                        ) * E("100")
                if market_value is not None:
                    account_value += market_value
                if cost_basis is not None:
                    account_cost += cost_basis
                breakdowns.append(
                    HoldingBreakdown(
                        security_id=str(holding.security_id),
                        ticker=security.ticker if security else None,
                        security_name=security.name if security else "Unknown",
                        security_type=(
                            str(security.security_type)
                            if security
                            else "other"
                        ),
                        quantity=holding.quantity,
                        cost_basis=cost_basis,
                        cost_basis_currency=holding.cost_basis_currency,
                        market_value=market_value,
                        price=price,
                        price_currency=holding.price_currency
                        or (latest.currency_code if latest else None),
                        currency_code=holding.currency_code,
                        unrealised_pl=unrealised_pl,
                        unrealised_pl_pct=unrealised_pl_pct,
                    )
                )
            total_value += account_value
            total_cost_basis += account_cost
            accounts.append(
                AccountPortfolioBreakdown(
                    account_id=account_id,
                    account_name=account.name if account else account_id,
                    account_type=(
                        str(account.account_type) if account else "unknown"
                    ),
                    holdings=breakdowns,
                    total_value=account_value,
                    total_cost_basis=account_cost,
                )
            )
        return PortfolioResponse(
            accounts=accounts,
            total_value=total_value,
            total_cost_basis=total_cost_basis,
        )

    async def get_holdings(
        self,
        tenant_id: str,
        *,
        account_id: str | None = None,
        security_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        """Return the latest holding snapshot per account and security."""
        from finance_sync.services.read_api import (
            HoldingItemResponse,
            HoldingsListResponse,
        )

        subq_conditions: list[Any] = [
            Holding.tenant_id == tenant_id,
            self._derived_condition(Holding),
        ]
        if as_of is not None:
            subq_conditions.append(Holding.observed_at <= as_of)
        latest = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_ts"),
            )
            .where(expression(*subq_conditions))
            .group_by(Holding.account_id, Holding.security_id)
            .subquery()
        )
        conditions: list[Any] = [
            Holding.tenant_id == tenant_id,
            Holding.account_id == latest.c.account_id,
            Holding.security_id == latest.c.security_id,
            Holding.observed_at == latest.c.latest_ts,
            self._derived_condition(Holding),
        ]
        if account_id is not None:
            conditions.append(Holding.account_id == account_id)
        if security_id is not None:
            conditions.append(Holding.security_id == security_id)
        result = await self._session.execute(
            select(Holding)
            .where(expression(*conditions))
            .order_by(Holding.account_id, Holding.security_id)
            .offset(offset)
            .limit(limit)
        )
        holdings: list[Holding] = list(result.scalars().all())
        count_result = await self._session.execute(
            select(func.count()).select_from(Holding).where(
                expression(*conditions)
            )
        )
        total: int = count_result.scalar() or 0
        if not holdings:
            return HoldingsListResponse(
                items=[],
                total=total,
                limit=limit,
                offset=offset,
                meta=CollectionMeta(as_of=None, freshness="unknown"),
            )
        account_ids = list({h.account_id for h in holdings})
        security_ids = list({h.security_id for h in holdings})
        accounts_result = await self._session.execute(
            select(Account).where(
                Account.id.in_(account_ids),
                Account.tenant_id == tenant_id,
                self._account_condition(),
            )
        )
        accounts = {str(a.id): a for a in accounts_result.scalars().all()}
        securities_result = await self._session.execute(
            select(Security).where(Security.id.in_(security_ids))
        )
        securities = {
            str(s.id): s for s in securities_result.scalars().all()
        }
        items: list[Any] = []
        for holding in holdings:
            security = securities.get(str(holding.security_id))
            market_value = holding.market_value
            if market_value is None and holding.price is not None:
                market_value = holding.quantity * holding.price
            unrealised_pl = None
            unrealised_pl_pct = None
            if holding.cost_basis is not None and market_value is not None:
                unrealised_pl = market_value - holding.cost_basis
                if holding.cost_basis != E("0"):
                    unrealised_pl_pct = (
                        unrealised_pl / holding.cost_basis
                    ) * E("100")
            account = accounts.get(str(holding.account_id))
            items.append(
                HoldingItemResponse(
                    account_id=str(holding.account_id),
                    account_name=account.name if account else None,
                    security_id=str(holding.security_id),
                    ticker=security.ticker if security else None,
                    security_name=security.name if security else "Unknown",
                    security_type=(
                        str(security.security_type) if security else "other"
                    ),
                    quantity=holding.quantity,
                    cost_basis=holding.cost_basis,
                    cost_basis_currency=holding.cost_basis_currency,
                    market_value=market_value,
                    price=holding.price,
                    price_currency=holding.price_currency,
                    currency_code=holding.currency_code,
                    observed_at=holding.observed_at,
                    unrealised_pl=unrealised_pl,
                    unrealised_pl_pct=unrealised_pl_pct,
                )
            )
        observed_at = max(h.observed_at for h in holdings)
        return HoldingsListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            meta=CollectionMeta(
                as_of=observed_at,
                freshness=freshness_for(observed_at),
            ),
        )
