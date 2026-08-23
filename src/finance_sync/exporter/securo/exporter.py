from __future__ import annotations

import asyncio
import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from finance_sync.exporter.securo.client import SecuroClient
from finance_sync.exporter.securo.config import SecuroConfig
from finance_sync.models import Account, Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SecuroExportResult:
    def __init__(
        self,
        *,
        status: str,
        files: list[Path],
        attempted: int,
        imported: int = 0,
        skipped: int = 0,
        error: str | None = None,
    ) -> None:
        self.status, self.files, self.transactions_attempted = (
            status,
            files,
            attempted,
        )
        (
            self.transactions_imported,
            self.transactions_skipped,
            self.error_message,
        ) = imported, skipped, error


class SecuroExporter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: SecuroConfig,
        tenant_id: str,
    ) -> None:
        self.session_factory, self.config, self.tenant_id = (
            session_factory,
            config,
            tenant_id,
        )

    async def _accounts(self, account_ids: list[str] | None) -> list[Account]:
        async with self.session_factory() as session:
            stmt = (
                select(Account)
                .where(
                    Account.tenant_id == self.tenant_id,
                    Account.is_active.is_(True),
                )
                .order_by(Account.name)
            )
            if account_ids:
                stmt = stmt.where(Account.id.in_(account_ids))
            return list((await session.execute(stmt)).scalars().all())

    async def _transactions(
        self, session: AsyncSession, account_id: str, since: datetime
    ) -> list[Transaction]:
        stmt = (
            select(Transaction)
            .where(
                Transaction.tenant_id == self.tenant_id,
                Transaction.account_id == account_id,
                Transaction.occurred_at >= since,
                Transaction.status.in_(["booked", "pending"]),
            )
            .order_by(Transaction.occurred_at, Transaction.id)
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    def csv_bytes(transactions: list[Transaction]) -> bytes:
        out = io.StringIO(newline="")
        writer = csv.DictWriter(
            out,
            fieldnames=[
                "date",
                "description",
                "amount",
                "type",
                "currency",
                "external_id",
                "payee",
                "notes",
            ],
        )
        writer.writeheader()
        for txn in transactions:
            amount = abs(txn.amount)
            writer.writerow(
                {
                    "date": txn.occurred_at.astimezone(UTC).date().isoformat(),
                    "description": txn.description or txn.transaction_type,
                    "amount": f"{amount:.2f}",
                    "type": "credit" if txn.amount > 0 else "debit",
                    "currency": txn.currency_code,
                    "external_id": (
                        f"finance-sync:{txn.provider_key}:"
                        f"{txn.external_transaction_id}"
                    ),
                    "payee": txn.description or "",
                    "notes": f"finance-sync transaction {txn.id}",
                }
            )
        return out.getvalue().encode("utf-8")

    async def run_export(
        self,
        *,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
        output_dir: str | None = None,
        push: bool = False,
    ) -> SecuroExportResult:
        since = since or datetime.now(UTC) - timedelta(days=90)
        accounts = await self._accounts(account_ids)
        destination = Path(output_dir or self.config.output_dir)
        await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
        files: list[Path] = []
        attempted = imported = skipped = 0
        try:
            async with self.session_factory() as session:
                client: SecuroClient | _NullClient = (
                    SecuroClient(self.config) if push else _NullClient()
                )
                async with client:
                    push_client = cast(SecuroClient, client)
                    remote_accounts: list[dict[str, Any]] = []
                    if push:
                        await push_client.login()
                        remote_accounts = await push_client.accounts()
                    for account in accounts:
                        txns = await self._transactions(
                            session, account.id, since
                        )
                        attempted += len(txns)
                        if not txns:
                            continue
                        content = self.csv_bytes(txns)
                        filename = (
                            f"securo_{account.id}_"
                            f"{since.date().isoformat()}.csv"
                        )
                        path = destination / filename
                        await asyncio.to_thread(path.write_bytes, content)
                        files.append(path)
                        if push:
                            name = self.config.account_name_overrides.get(
                                account.id, account.name
                            )
                            remote = next(
                                (
                                    item
                                    for item in remote_accounts
                                    if item.get("name") == name
                                    or item.get("display_name") == name
                                ),
                                None,
                            )
                            if (
                                remote is None
                                and self.config.auto_create_accounts
                            ):
                                remote = await push_client.create_account(
                                    name=name,
                                    currency=account.currency_code,
                                    account_type=_securo_account_type(
                                        account.account_type
                                    ),
                                )
                                remote_accounts.append(remote)
                            if remote is None:
                                message = (
                                    "Geen Securo-account gevonden voor "
                                    f"{account.name!r}"
                                )
                                raise RuntimeError(message)
                            result = await push_client.import_csv(
                                content=content,
                                filename=path.name,
                                account_id=str(remote["id"]),
                                mapping={
                                    "date": "date",
                                    "description": "description",
                                    "amount": "amount",
                                    "type": "type",
                                    "currency": "currency",
                                    "external_id": "external_id",
                                    "payee": "payee",
                                    "notes": "notes",
                                },
                            )
                            summary = result["import"]
                            imported += int(summary.get("imported", 0))
                            skipped += int(summary.get("skipped", 0))
            return SecuroExportResult(
                status="completed",
                files=files,
                attempted=attempted,
                imported=imported,
                skipped=skipped,
            )
        except Exception as exc:
            return SecuroExportResult(
                status="failed",
                files=files,
                attempted=attempted,
                imported=imported,
                skipped=skipped,
                error=str(exc),
            )


class _NullClient:
    async def __aenter__(self) -> _NullClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _securo_account_type(account_type: Any) -> str:
    value = str(account_type).lower()
    return {
        "savings": "savings",
        "credit": "credit_card",
        "loan": "loan",
        "cash": "cash",
    }.get(value, "checking")
