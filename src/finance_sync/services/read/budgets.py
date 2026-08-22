"""Query-budget contracts for read endpoints.

Budgets are intentionally small, explicit data objects.  They can be used by
integration tests without coupling those tests to a particular database
driver or timing environment.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryBudget:
    """Maximum number of SQL statements allowed for one read operation."""

    operation: str
    max_queries: int

    def assert_within(self, query_count: int) -> None:
        """Raise a useful assertion when an endpoint regresses to N+1."""
        if query_count > self.max_queries:
            message = (
                f"{self.operation} used {query_count} queries; "
                f"budget is {self.max_queries}"
            )
            raise AssertionError(message)


# These are intentionally query-count budgets, not latency SLOs.  Latency
# depends on CI hardware and database placement; query count is deterministic.
READ_QUERY_BUDGETS: dict[str, QueryBudget] = {
    "portfolio": QueryBudget("portfolio", 4),
    "holdings": QueryBudget("holdings", 4),
    # Count + page + one set-based latest-price query.
    "securities": QueryBudget("securities", 3),
    "latest_prices": QueryBudget("latest_prices", 1),
    "net_worth": QueryBudget("net_worth", 4),
    "cashflow": QueryBudget("cashflow", 4),
    "portfolio_history": QueryBudget("portfolio_history", 2),
    "net_worth_history": QueryBudget("net_worth_history", 2),
    "cashflow_history": QueryBudget("cashflow_history", 4),
}
