"""Transaction ingestion stage for the sync pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from finance_sync.connectors.models import (
        CanonicalTransactionData,
        SecurityReference,
    )
    from finance_sync.db.uow import UnitOfWork


class TransactionStageWriter(Protocol):
    """Persistence and security-resolution boundary for transactions."""

    async def resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[object | None, str | None]: ...

    async def persist_transaction(
        self,
        uow: UnitOfWork,
        transaction: CanonicalTransactionData,
        account_id: str,
        *,
        security_id: str | None = None,
        connection_id: str | None = None,
    ) -> object: ...

    async def persist_transactions_batch(
        self,
        uow: UnitOfWork,
        transactions: list[CanonicalTransactionData],
        account_id: str,
        *,
        security_ids: list[str | None] | None = None,
        connection_id: str | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TransactionStageResult:
    """Counters and unresolved security keys produced by the stage."""

    count: int
    unresolved_keys: frozenset[str]


class TransactionSyncStage:
    """Resolve and persist transactions without committing the UoW."""

    def __init__(self, writer: TransactionStageWriter) -> None:
        self._writer = writer

    async def run(
        self,
        uow: UnitOfWork,
        transactions: list[CanonicalTransactionData],
        *,
        account_id: str,
        provider_type: str,
        connection_id: str | None = None,
    ) -> TransactionStageResult:
        unresolved: set[str] = set()
        security_ids: list[str | None] = []
        for transaction in transactions:
            security_id: str | None = None
            if transaction.security_reference is not None:
                (
                    security,
                    unresolved_key,
                ) = await self._writer.resolve_security_reference(
                    uow,
                    provider_type,
                    transaction.security_reference,
                )
                security_id = str(getattr(security, "id", "")) or None
                if unresolved_key:
                    unresolved.add(unresolved_key)
            security_ids.append(security_id)
        if hasattr(type(self._writer), "persist_transactions_batch"):
            count = await self._writer.persist_transactions_batch(
                uow,
                transactions,
                account_id,
                security_ids=security_ids,
                connection_id=connection_id,
            )
        else:
            # Writers that predate the batch surface (test doubles, the
            # writer-only SyncPersistence mode) fall back to per-row.
            count = 0
            for index, transaction in enumerate(transactions):
                await self._writer.persist_transaction(
                    uow,
                    transaction,
                    account_id,
                    security_id=(
                        security_ids[index]
                        if index < len(security_ids)
                        else None
                    ),
                    connection_id=connection_id,
                )
                count += 1
        return TransactionStageResult(
            count=count,
            unresolved_keys=frozenset(unresolved),
        )
