"""Backward-compatible re-exports for the Actual Budget client.

Users upgrading from the old layout (``finance_sync.exporter.client``)
can still import ``ActualBudgetClient`` and error classes here.
"""

from finance_sync.exporter.actual_budget.client import (
    ActualBudgetAccountError,
    ActualBudgetClient,
    ActualBudgetConnectionError,
    ActualBudgetError,
)

__all__ = [
    "ActualBudgetAccountError",
    "ActualBudgetClient",
    "ActualBudgetConnectionError",
    "ActualBudgetError",
]
