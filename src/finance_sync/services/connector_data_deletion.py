"""Preview and delete data owned by one connector connection.

Connector credentials are deliberately not parents of canonical financial
data in the database.  This service therefore performs the ownership walk
explicitly, keeping deletion tenant-scoped and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select

from finance_sync.exporter.actual_budget.models import (
    ActualBudgetAccountMapping,
    ExportDelivery,
)
from finance_sync.exporter.wealthfolio.models import (
    WealthfolioAccountMapping,
    WealthfolioDelivery,
)
from finance_sync.models import (
    Account,
    Balance,
    CardTransaction,
    DetectedSubscription,
    Holding,
    HoldingRelevanceItem,
    ReconciliationResult,
    ScheduledPayment,
    TaxLot,
    Transaction,
    TransactionAnnotation,
    TransactionOverride,
    TransactionSourceReference,
    TransactionSplit,
)
from finance_sync.models.connector_state import ConnectorState
from finance_sync.models.import_run import ImportRun
from finance_sync.models.sync_cursor import SyncCursor
from finance_sync.models.sync_run import SyncRun
from finance_sync.models.sync_schedule import SCOPE_INGESTION, SyncSchedule
from finance_sync.models.transaction_event import TransactionLifecycleEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.models.credential import Credential


@dataclass(frozen=True)
class ConnectorDeletionPreview:
    provider_key: str
    connection_id: str
    accounts: int
    transactions: int
    card_transactions: int
    holdings: int
    balances: int
    other_records: int
    legacy_records_warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "connection_id": self.connection_id,
            "accounts": self.accounts,
            "transactions": self.transactions,
            "card_transactions": self.card_transactions,
            "holdings": self.holdings,
            "balances": self.balances,
            "other_records": self.other_records,
            "legacy_records_warning": self.legacy_records_warning,
        }


class ConnectorDataDeletionService:
    """Delete canonical and derived data belonging to one connection."""

    def __init__(self, session: AsyncSession, tenant_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def _account_ids(self, connection_id: str) -> list[str]:
        result = await self.session.scalars(
            select(Account.id).where(
                Account.tenant_id == self.tenant_id,
                Account.connection_id == connection_id,
            )
        )
        return [str(value) for value in result]

    async def _count_connection(self, model: Any, connection_id: str) -> int:
        conditions = [model.connection_id == connection_id]
        if hasattr(model, "tenant_id"):
            conditions.append(model.tenant_id == self.tenant_id)
        return int(
            await self.session.scalar(
                select(func.count()).select_from(model).where(*conditions)
            )
            or 0
        )

    async def _count_legacy(self, model: Any, provider_key: str) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == self.tenant_id,
                    model.provider_key == provider_key,
                    model.connection_id.is_(None),
                )
            )
            or 0
        )

    async def preview(self, credential: Credential) -> ConnectorDeletionPreview:
        connection_id = str(credential.id)
        account_ids = await self._account_ids(connection_id)

        async def count_accounts_table(model: Any) -> int:
            return (
                int(
                    await self.session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.account_id.in_(account_ids))
                    )
                    or 0
                )
                if account_ids
                else 0
            )

        async def count_transaction_table(model: Any) -> int:
            if not account_ids:
                return 0
            transaction_ids = select(Transaction.id).where(
                Transaction.tenant_id == self.tenant_id,
                (Transaction.account_id.in_(account_ids))
                | (Transaction.connection_id == connection_id),
            )
            column = getattr(model, "transaction_id", None)
            if column is None:
                return 0
            return int(
                await self.session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(column.in_(transaction_ids))
                )
                or 0
            )

        account_scoped_models = (
            Balance,
            Holding,
            TaxLot,
            ScheduledPayment,
            DetectedSubscription,
            ReconciliationResult,
            HoldingRelevanceItem,
            WealthfolioAccountMapping,
            WealthfolioDelivery,
            ActualBudgetAccountMapping,
            ExportDelivery,
        )
        account_scoped_counts = sum(
            [
                await count_accounts_table(model)
                for model in account_scoped_models
            ]
        )
        transaction_scoped_counts = sum(
            [
                await count_transaction_table(model)
                for model in (
                    TransactionLifecycleEvent,
                    TransactionAnnotation,
                    TransactionSourceReference,
                    TransactionSplit,
                    TransactionOverride,
                )
            ]
        )
        connection_scoped_models = (
            SyncCursor,
            SyncRun,
            ConnectorState,
            ScheduledPayment,
        )
        connection_counts = sum(
            [
                await self._count_connection(model, connection_id)
                for model in connection_scoped_models
            ]
        )
        transaction_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(
                    Transaction.tenant_id == self.tenant_id,
                    (Transaction.account_id.in_(account_ids))
                    | (Transaction.connection_id == connection_id),
                )
            )
            or 0
        )
        card_conditions = [
            CardTransaction.tenant_id == self.tenant_id,
            CardTransaction.connection_id == connection_id,
        ]
        if account_ids:
            card_conditions[-1] = (
                CardTransaction.connection_id == connection_id
            ) | (CardTransaction.account_id.in_(account_ids))
        card_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(CardTransaction)
                .where(*card_conditions)
            )
            or 0
        )
        import_runs = await self._count_connection(ImportRun, connection_id)
        schedule_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(SyncSchedule)
                .where(
                    SyncSchedule.tenant_id == self.tenant_id,
                    SyncSchedule.scope == SCOPE_INGESTION,
                    SyncSchedule.target_id == connection_id,
                )
            )
            or 0
        )
        legacy_count = sum(
            [
                await self._count_legacy(model, credential.provider_key)
                for model in (Account, Transaction, CardTransaction)
            ]
        )
        return ConnectorDeletionPreview(
            provider_key=credential.provider_key,
            connection_id=connection_id,
            accounts=len(account_ids),
            transactions=transaction_count,
            card_transactions=card_count,
            holdings=await count_accounts_table(Holding),
            balances=await count_accounts_table(Balance),
            other_records=(
                account_scoped_counts
                + transaction_scoped_counts
                + connection_counts
                + import_runs
                + schedule_count
            ),
            legacy_records_warning=(
                "Data without a connection_id is not included and will be kept."
                if legacy_count
                else None
            ),
        )

    async def delete(self, credential: Credential) -> None:
        """Delete all connection-owned rows; caller owns the transaction."""
        connection_id = str(credential.id)
        account_ids = await self._account_ids(connection_id)

        if account_ids:
            transaction_ids = {
                str(value)
                for value in await self.session.scalars(
                    select(Transaction.id).where(
                        Transaction.tenant_id == self.tenant_id,
                        (Transaction.account_id.in_(account_ids))
                        | (Transaction.connection_id == connection_id),
                    )
                )
            }
            # Explicit children first: several account/transaction FKs are
            # RESTRICT by design, and this also works on SQLite test DBs.
            for model in (
                TransactionLifecycleEvent,
                TransactionAnnotation,
                TransactionSourceReference,
                TransactionSplit,
                TransactionOverride,
                TaxLot,
            ):
                transaction_column = getattr(model, "transaction_id", None)
                if transaction_ids and transaction_column is not None:
                    await self.session.execute(
                        delete(model).where(
                            transaction_column.in_(transaction_ids)
                        )
                    )
            for model in (
                WealthfolioAccountMapping,
                WealthfolioDelivery,
                ActualBudgetAccountMapping,
                ExportDelivery,
                Holding,
                Balance,
                TaxLot,
                ScheduledPayment,
                DetectedSubscription,
                ReconciliationResult,
                HoldingRelevanceItem,
                CardTransaction,
                Transaction,
            ):
                account_column = getattr(model, "account_id", None)
                connection_column = getattr(model, "connection_id", None)
                await self.session.execute(
                    delete(model).where(
                        (account_column.in_(account_ids))
                        if account_column is not None
                        else connection_column == connection_id
                    )
                )
            await self.session.execute(
                delete(Account).where(Account.id.in_(account_ids))
            )

        # Card/transaction rows can exist without an account link.  They are
        # still unambiguously owned when their connection_id matches.
        await self.session.execute(
            delete(CardTransaction).where(
                CardTransaction.tenant_id == self.tenant_id,
                CardTransaction.connection_id == connection_id,
            )
        )
        await self.session.execute(
            delete(Transaction).where(
                Transaction.tenant_id == self.tenant_id,
                Transaction.connection_id == connection_id,
            )
        )

        # These tables intentionally retain no financial history and are
        # scoped directly to the deleted connection.
        for model in (SyncCursor, SyncRun, ConnectorState, ImportRun):
            await self.session.execute(
                delete(model).where(model.connection_id == connection_id)
            )
        schedules = await self.session.scalars(
            select(SyncSchedule).where(
                SyncSchedule.tenant_id == self.tenant_id,
                SyncSchedule.scope == SCOPE_INGESTION,
                SyncSchedule.target_id == connection_id,
            )
        )
        for schedule in schedules:
            # Keep the row for operational/audit visibility, but make it
            # impossible for the worker to plan a run for the deleted source.
            schedule.enabled = False
            schedule.next_run_at = None
        await self.session.delete(credential)
        await self.session.flush()
