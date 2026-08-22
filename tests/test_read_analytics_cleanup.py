"""Characterization tests for the analytics read boundary."""

from pathlib import Path

from finance_sync.services.read.analytics import AnalyticsReadService
from finance_sync.services.read.analytics_history import (
    AnalyticsHistoryReadService,
)
from finance_sync.services.read_api import ReadService

READ_API = Path(__file__).parents[1] / "src/finance_sync/services/read_api.py"


def test_analytics_queries_are_delegated_without_legacy_sql() -> None:
    source = READ_API.read_text(encoding="utf-8")

    assert "select(" not in source
    assert "Legacy" not in source
    assert ReadService.get_net_worth is AnalyticsReadService.get_net_worth
    assert ReadService.get_cashflow is AnalyticsReadService.get_cashflow


def test_history_queries_have_an_explicit_component_owner() -> None:
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


def test_history_component_preserves_operational_query_contract() -> None:
    assert AnalyticsHistoryReadService.get_portfolio_history.__annotations__[
        "return"
    ]
    assert AnalyticsHistoryReadService.get_net_worth_history.__annotations__[
        "return"
    ]
    assert AnalyticsHistoryReadService.get_cashflow_history.__annotations__[
        "return"
    ]
