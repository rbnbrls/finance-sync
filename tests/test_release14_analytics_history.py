"""Release 14 analytics-history boundary and budget contracts."""

from finance_sync.services.read.analytics_history import (
    AnalyticsHistoryReadService,
)
from finance_sync.services.read.budgets import READ_QUERY_BUDGETS
from finance_sync.services.read.operational import OperationalReadService
from finance_sync.services.read_api import ReadService


def test_history_operations_are_owned_by_dedicated_component() -> None:
    assert (
        ReadService.get_portfolio_history
        is AnalyticsHistoryReadService.get_portfolio_history
    )
    assert (
        ReadService.get_net_worth_history
        is AnalyticsHistoryReadService.get_net_worth_history
    )
    assert (
        ReadService.get_cashflow_history
        is AnalyticsHistoryReadService.get_cashflow_history
    )
    assert (
        OperationalReadService.get_portfolio_history
        is not ReadService.get_portfolio_history
    )


def test_history_query_budgets_are_explicit() -> None:
    for operation in (
        "portfolio_history",
        "net_worth_history",
        "cashflow_history",
    ):
        assert operation in READ_QUERY_BUDGETS
        assert READ_QUERY_BUDGETS[operation].max_queries > 0
