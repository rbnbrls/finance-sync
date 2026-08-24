"""End-to-end exporter from finance-sync into Firefly III."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from finance_sync.exporter.firefly.client import (
    FireflyAPIError,
    FireflyClient,
    FireflyClientConfig,
)
from finance_sync.exporter.firefly.transaction_mapper import map_transaction
from finance_sync.exporter.models import ExportRun
from finance_sync.models import Account, Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.exporter.firefly.config import FireflyConfig


class FireflyExportResult:
    """Summary of one Firefly export attempt."""

    __slots__ = (
        "accounts_mapped",
        "duration_s",
        "error_message",
        "run_id",
        "status",
        "transactions_attempted",
        "transactions_exported",
        "transactions_failed",
    )

    def __init__(
        self,
        *,
        status: str,
        accounts_mapped: int = 0,
        transactions_attempted: int = 0,
        transactions_exported: int = 0,
        transactions_failed: int = 0,
        error_message: str | None = None,
        duration_s: float = 0.0,
        run_id: str | None = None,
    ) -> None:
        self.status = status
        self.accounts_mapped = accounts_mapped
        self.transactions_attempted = transactions_attempted
        self.transactions_exported = transactions_exported
        self.transactions_failed = transactions_failed
        self.error_message = error_message
        self.duration_s = duration_s
        self.run_id = run_id


class FireflyExporter:
    """Push canonical accounts and transactions to Firefly III.

    Firefly's duplicate-hash guard is deliberately enabled. Together with
    the external ID and transaction notes this makes a retry after a network
    failure safe even when the remote write succeeded before the timeout.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        firefly_config: FireflyConfig,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._config = firefly_config
        self._tenant_id = tenant_id

    async def run_export(
        self,
        *,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
        max_transactions: int | None = None,
    ) -> FireflyExportResult:
        started = datetime.now(UTC)
        run = ExportRun(
            status="running", started_at=started, exporter_type="firefly"
        )
        async with self._session_factory() as session:
            session.add(run)
            await session.flush()
            await session.commit()
        attempted = exported = failed = mapped = 0
        effective_since = since or started - timedelta(days=90)
        client: FireflyClient | None = None
        try:
            if not self._config.access_token:
                msg = "FIREFLY_ACCESS_TOKEN is required for export"
                raise ValueError(msg)
            client = FireflyClient(
                FireflyClientConfig(
                    base_url=self._config.server_url,
                    access_token=self._config.access_token,
                    request_timeout=self._config.request_timeout,
                    verify_ssl=self._config.verify_ssl,
                )
            )
            accounts = await self._load_accounts(account_ids, effective_since)
            async with client:
                for account in accounts:
                    remote_name = self._config.account_name_overrides.get(
                        str(account.id), account.name
                    )
                    await client.ensure_asset_account(
                        name=remote_name,
                        currency_code=str(
                            account.currency_code
                            or self._config.default_currency
                        ),
                    )
                    mapped += 1
                    transactions = await self._load_transactions(
                        account.id, effective_since
                    )
                    if max_transactions is not None:
                        transactions = transactions[:max_transactions]
                    for transaction in transactions:
                        attempted += 1
                        payload = map_transaction(
                            transaction,
                            account_name=remote_name,
                            import_tag=self._config.import_tag,
                        )
                        try:
                            await client.store_transaction(payload)
                            exported += 1
                        except FireflyAPIError as exc:
                            # Firefly returns 422 for a duplicate hash. A
                            # duplicate is a successful idempotent outcome.
                            if (
                                "duplicate" in str(exc).lower()
                                or "already" in str(exc).lower()
                            ):
                                exported += 1
                            else:
                                failed += 1
                                raise
            status = "completed"
            error = None
        except Exception as exc:
            status = "failed"
            error = f"{exc}\n{traceback.format_exc(limit=3)}"
        finally:
            if client is not None:
                await client.close()
        await self._finish_run(run, status, attempted, exported, failed, error)
        return FireflyExportResult(
            status=status,
            accounts_mapped=mapped,
            transactions_attempted=attempted,
            transactions_exported=exported,
            transactions_failed=failed,
            error_message=error,
            duration_s=(datetime.now(UTC) - started).total_seconds(),
            run_id=str(run.id),
        )

    async def _load_accounts(
        self, account_ids: list[str] | None, since: datetime
    ) -> list[Account]:
        async with self._session_factory() as session:
            stmt = select(Account).where(
                Account.tenant_id == self._tenant_id,
                Account.is_active.is_(True),
            )
            if account_ids:
                stmt = stmt.where(Account.id.in_(account_ids))
            stmt = stmt.where(
                select(Transaction.id)
                .where(
                    Transaction.account_id == Account.id,
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.occurred_at >= since,
                )
                .exists()
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _load_transactions(
        self, account_id: str, since: datetime
    ) -> list[Transaction]:
        async with self._session_factory() as session:
            stmt = (
                select(Transaction)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.account_id == account_id,
                    Transaction.occurred_at >= since,
                    Transaction.status.in_(["booked", "pending"]),
                )
                .order_by(Transaction.occurred_at, Transaction.id)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _finish_run(
        self,
        run: ExportRun,
        status: str,
        attempted: int,
        exported: int,
        failed: int,
        error: str | None,
    ) -> None:
        async with self._session_factory() as session:
            stored = await session.merge(run)
            stored.status = status
            stored.completed_at = datetime.now(UTC)
            stored.transactions_attempted = attempted
            stored.transactions_exported = exported
            stored.transactions_failed = failed
            stored.error_message = error
            await session.commit()
