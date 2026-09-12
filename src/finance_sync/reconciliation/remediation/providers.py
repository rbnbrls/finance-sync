"""Provider strategy extension point; no provider branches in the executor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from finance_sync.reconciliation.remediation.verification import (
        VerificationResult,
    )


class RemediationStrategy(Protocol):
    key: str
    endpoint_family: str
    quota_cost: int

    async def execute(self, item: Any, connector: Any) -> Any: ...

    async def verify(self, item: Any) -> VerificationResult: ...


class StrategyRegistry:
    def __init__(
        self, strategies: list[RemediationStrategy] | None = None
    ) -> None:
        self._strategies = {
            strategy.key: strategy for strategy in strategies or []
        }

    def register(self, strategy: RemediationStrategy) -> None:
        self._strategies[strategy.key] = strategy

    def get(self, key: str) -> RemediationStrategy | None:
        return self._strategies.get(key)
