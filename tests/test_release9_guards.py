"""Regression guards for Release 9 read contracts and budgets."""

import pytest

from finance_sync.services.read.budgets import (
    READ_QUERY_BUDGETS,
    QueryBudget,
)


def test_query_budgets_are_explicit_and_latest_prices_are_set_based() -> None:
    assert set(READ_QUERY_BUDGETS) == {
        "portfolio",
        "holdings",
        "securities",
        "latest_prices",
        "net_worth",
        "cashflow",
        "portfolio_history",
        "net_worth_history",
        "cashflow_history",
    }
    assert READ_QUERY_BUDGETS["latest_prices"].max_queries == 1


def test_query_budget_rejects_regression() -> None:
    budget = QueryBudget("portfolio", 4)
    budget.assert_within(4)
    with pytest.raises(AssertionError, match="budget is 4"):
        budget.assert_within(5)
