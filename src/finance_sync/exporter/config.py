"""Backward-compatible re-exports for the Actual Budget config.

Users upgrading from the old layout (``finance_sync.exporter.config``)
can still import ``ActualBudgetConfig`` here.
"""

from finance_sync.exporter.actual_budget.config import (
    ActualBudgetConfig,
)

__all__ = [
    "ActualBudgetConfig",
]
