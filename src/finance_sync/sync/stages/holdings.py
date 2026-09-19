"""Holdings ingestion stage for the sync pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from finance_sync.connectors.models import (
        CanonicalHoldingData,
        SecurityReference,
    )
    from finance_sync.db.uow import UnitOfWork


class HoldingsStageWriter(Protocol):
    """Security resolution and persistence boundary for holdings."""

    async def resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[object | None, str | None]: ...

    async def persist_holding(
        self,
        uow: UnitOfWork,
        holding: CanonicalHoldingData,
        account_id: str,
        security_id: str,
    ) -> object: ...

    async def persist_holdings_batch(
        self,
        uow: UnitOfWork,
        holdings: list[CanonicalHoldingData],
        account_id: str,
        *,
        security_ids: list[str],
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class HoldingsStageResult:
    """Counters and unresolved security keys produced by the stage."""

    count: int
    unresolved_keys: frozenset[str]


class HoldingsSyncStage:
    """Resolve and persist holdings without committing the UoW."""

    def __init__(self, writer: HoldingsStageWriter) -> None:
        self._writer = writer

    async def run(
        self,
        uow: UnitOfWork,
        holdings: list[CanonicalHoldingData],
        *,
        account_id: str,
        provider_key: str,
    ) -> HoldingsStageResult:
        unresolved: set[str] = set()
        persisted = 0
        resolved_by_snapshot: dict[
            tuple[str, object], CanonicalHoldingData
        ] = {}
        security_ids: list[str] = []
        for holding in holdings:
            (
                security,
                unresolved_key,
            ) = await self._writer.resolve_security_reference(
                uow, provider_key, holding.security_reference
            )
            if security is None:
                if unresolved_key:
                    unresolved.add(unresolved_key)
                continue
            security_id = str(getattr(security, "id", ""))
            if not security_id:
                continue
            # Older connector/test doubles may not expose a snapshot time;
            # retain the legacy single-row behaviour for those callers.
            snapshot_key = (security_id, getattr(holding, "observed_at", None))
            existing = resolved_by_snapshot.get(snapshot_key)
            if existing is None:
                resolved_by_snapshot[snapshot_key] = holding
                continue
            # Providers can return more than one position for the same
            # canonical security (e.g. Trading212's venue-specific rows).
            # PostgreSQL rejects those duplicates inside one INSERT ... ON
            # CONFLICT statement.  Fold them into one snapshot before the
            # persistence boundary, preserving the accounting totals.
            resolved_by_snapshot[snapshot_key] = existing.model_copy(
                update={
                    "quantity": existing.quantity + holding.quantity,
                    "cost_basis": _sum_optional(
                        existing.cost_basis, holding.cost_basis
                    ),
                    "market_value": _sum_optional(
                        existing.market_value, holding.market_value
                    ),
                }
            )

        resolved_holdings = list(resolved_by_snapshot.values())
        security_ids = [
            security_id for security_id, _observed_at in resolved_by_snapshot
        ]
        if not resolved_holdings:
            return HoldingsStageResult(
                count=0,
                unresolved_keys=frozenset(unresolved),
            )
        if hasattr(type(self._writer), "persist_holdings_batch"):
            persisted = await self._writer.persist_holdings_batch(
                uow,
                resolved_holdings,
                account_id,
                security_ids=security_ids,
            )
        else:
            # Writers that predate the batch surface (test doubles, the
            # writer-only SyncPersistence mode) fall back to per-row.
            persisted = 0
            for index, holding in enumerate(resolved_holdings):
                await self._writer.persist_holding(
                    uow,
                    holding,
                    account_id,
                    security_ids[index],
                )
                persisted += 1
        return HoldingsStageResult(
            count=persisted,
            unresolved_keys=frozenset(unresolved),
        )


def _sum_optional(
    left: Decimal | None, right: Decimal | None
) -> Decimal | None:
    """Add reported monetary values without inventing missing values."""
    if left is None:
        return right
    if right is None:
        return left
    return left + right
