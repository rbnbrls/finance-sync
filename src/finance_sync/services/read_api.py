"""Compatibility facade for the read-only API service.

Response schemas remain importable from this module for existing API and
integration callers. Query implementations live in focused read components.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from finance_sync.services.read.accounts import AccountReadService
from finance_sync.services.read.analytics import AnalyticsReadService
from finance_sync.services.read.analytics_history import (
    AnalyticsHistoryReadService,
)
from finance_sync.services.read.operational import OperationalReadService
from finance_sync.services.read.portfolio import PortfolioReadService
from finance_sync.services.read.schemas import (
    AccountDetailResponse,
    AccountPortfolioBreakdown,
    AccountSummary,
    BalanceListResponse,
    BalanceResponse,
    CardTransactionListResponse,
    CardTransactionResponse,
    CashflowHistoryEntry,
    CashflowHistoryResponse,
    CashflowResponse,
    CollectionMeta,
    DividendListResponse,
    HoldingBreakdown,
    HoldingItemResponse,
    HoldingsListResponse,
    NetWorthHistoryEntry,
    NetWorthHistoryResponse,
    NetWorthResponse,
    PortfolioHistoryEntry,
    PortfolioHistoryResponse,
    PortfolioResponse,
    ScheduledPaymentListResponse,
    ScheduledPaymentResponse,
    SecurityInfo,
    SecurityListResponse,
    SecurityPriceListResponse,
    SecurityPriceResponse,
    SyncRunListResponse,
    SyncRunResponse,
    SyncRunStatusCount,
    TopLevelPriceListResponse,
    TopLevelTransactionListResponse,
    TransactionListResponse,
    TransactionResponse,
)
from finance_sync.services.read.securities import SecuritiesReadService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.services.visibility import ReadScope


class ReadService(
    AccountReadService,
    PortfolioReadService,
    SecuritiesReadService,
    AnalyticsReadService,
    AnalyticsHistoryReadService,
    OperationalReadService,
):
    """Stable read-service API composed from domain-specific components."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        scope: ReadScope | None = None,
    ) -> None:
        AccountReadService.__init__(self, session, scope=scope)


__all__ = [
    "AccountDetailResponse",
    "AccountPortfolioBreakdown",
    "AccountSummary",
    "BalanceListResponse",
    "BalanceResponse",
    "CardTransactionListResponse",
    "CardTransactionResponse",
    "CashflowHistoryEntry",
    "CashflowHistoryResponse",
    "CashflowResponse",
    "CollectionMeta",
    "DividendListResponse",
    "HoldingBreakdown",
    "HoldingItemResponse",
    "HoldingsListResponse",
    "NetWorthHistoryEntry",
    "NetWorthHistoryResponse",
    "NetWorthResponse",
    "PortfolioHistoryEntry",
    "PortfolioHistoryResponse",
    "PortfolioResponse",
    "ReadService",
    "ScheduledPaymentListResponse",
    "ScheduledPaymentResponse",
    "SecurityInfo",
    "SecurityListResponse",
    "SecurityPriceListResponse",
    "SecurityPriceResponse",
    "SyncRunListResponse",
    "SyncRunResponse",
    "SyncRunStatusCount",
    "TopLevelPriceListResponse",
    "TopLevelTransactionListResponse",
    "TransactionListResponse",
    "TransactionResponse",
]
