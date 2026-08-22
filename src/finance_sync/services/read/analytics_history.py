"""Portfolio, net-worth and cashflow history read boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from finance_sync.services.read.operational import OperationalReadService

if TYPE_CHECKING:
    from datetime import datetime

    from finance_sync.services.read.schemas import (
        CashflowHistoryResponse,
        NetWorthHistoryResponse,
        PortfolioHistoryResponse,
    )


class AnalyticsHistoryReadService(OperationalReadService):
    """Own the public history operations while preserving query behavior."""

    async def get_portfolio_history(
        self,
        tenant_id: str,
        *,
        limit: int = 90,
        offset: int = 0,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> PortfolioHistoryResponse:
        return await super().get_portfolio_history(
            tenant_id,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
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
        return await super().get_net_worth_history(
            tenant_id,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
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
        return await super().get_cashflow_history(
            tenant_id,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
            account_id=account_id,
        )


__all__ = ["AnalyticsHistoryReadService"]
