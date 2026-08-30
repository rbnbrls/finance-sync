"""Tenant-scoped native YNAB exporter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from finance_sync.exporter.ynab.client import YNABClient
from finance_sync.exporter.ynab.config import YNABConfig
from finance_sync.exporter.ynab.transaction_mapper import map_transaction
from finance_sync.models import Account, Transaction
from finance_sync.services.destination_references import (
    record_destination_reference,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class YNABExporter:
    """Export canonical transactions using YNAB's native bulk endpoint."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: YNABConfig,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._tenant_id = tenant_id

    async def run_export(
        self,
        *,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        start = since or datetime.now(UTC) - timedelta(days=90)
        async with self._session_factory() as session:
            account_stmt = select(Account).where(
                Account.tenant_id == self._tenant_id,
                Account.is_active.is_(True),
            )
            if account_ids:
                account_stmt = account_stmt.where(Account.id.in_(account_ids))
            accounts = list(
                (await session.execute(account_stmt)).scalars().all()
            )
            account_by_id = {str(account.id): account for account in accounts}
            transaction_stmt = select(Transaction).where(
                Transaction.tenant_id == self._tenant_id,
                Transaction.account_id.in_(list(account_by_id)),
                Transaction.occurred_at >= start,
                Transaction.status.in_(["booked", "pending"]),
            )
            transactions = list(
                (await session.execute(transaction_stmt)).scalars().all()
            )

        payload: list[dict[str, Any]] = []
        payload_sources: list[Transaction] = []
        for transaction in transactions:
            remote_account = self._config.account_map.get(
                str(transaction.account_id)
            )
            if not remote_account:
                continue
            suggestion: Any = getattr(transaction, "cashflow_suggestion", None)
            if isinstance(suggestion, dict):
                mapping = cast(dict[str, Any], suggestion)
                suggestion = mapping.get("value") or mapping.get("category")
            category_id = self._config.category_map.get(
                str(getattr(suggestion, "value", suggestion))
            )
            transfer_account_id = self._config.transfer_account_map.get(
                str(transaction.counterparty_account_reference or "")
            )
            payload.append(
                map_transaction(
                    transaction,
                    account_id=remote_account,
                    category_id=category_id,
                    transfer_account_id=transfer_account_id,
                )
            )
            payload_sources.append(transaction)

        if not payload:
            return {"attempted": 0, "imported": 0, "skipped": len(transactions)}
        async with YNABClient(self._config) as client:
            result = await client.import_transactions(payload)
        remote_ids = result.get("data", {}).get("transaction_ids", [])
        for transaction, mapped, remote_id in zip(
            payload_sources, payload, remote_ids, strict=False
        ):
            if not isinstance(remote_id, str):
                continue
            await record_destination_reference(
                self._session_factory,
                tenant_id=self._tenant_id,
                destination_type="ynab",
                transaction_id=str(transaction.id),
                canonical_key=(
                    f"{transaction.provider_key}:"
                    f"{transaction.external_transaction_id}"
                ),
                destination_object_id=remote_id,
                idempotency_key=str(mapped["import_id"]),
                source_revision=getattr(transaction, "revision", None),
            )
        return {
            "attempted": len(payload),
            "imported": len(remote_ids),
            "skipped": len(transactions) - len(payload),
            "response": result,
        }
