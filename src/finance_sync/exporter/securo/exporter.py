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
from finance_sync.models import Account, Holding, Security, Transaction

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
        holdings_attempted: int = 0,
        holdings_imported: int = 0,
        holdings_skipped: int = 0,
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
        self.holdings_attempted = holdings_attempted
        self.holdings_imported = holdings_imported
        self.holdings_skipped = holdings_skipped


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

    async def _holdings(
        self, session: AsyncSession, account_id: str, since: datetime
    ) -> list[tuple[Holding, Security]]:
        """Load the latest position for every security in an account."""
        stmt = (
            select(Holding, Security)
            .join(Security, Security.id == Holding.security_id)
            .where(
                Holding.tenant_id == self.tenant_id,
                Holding.account_id == account_id,
                Holding.observed_at >= since,
            )
            .order_by(Holding.security_id, Holding.observed_at.desc())
        )
        rows = list((await session.execute(stmt)).all())
        latest: dict[str, tuple[Holding, Security]] = {}
        for holding, security in rows:
            latest.setdefault(str(holding.security_id), (holding, security))
        return list(latest.values())

    async def _push_holdings(
        self,
        client: SecuroClient,
        positions: list[tuple[Holding, Security]],
        remote_assets: list[dict[str, Any]],
    ) -> tuple[int, int]:
        created = updated = 0
        for holding, security in positions:
            payload = self._asset_payload(holding, security)
            remote = next(
                (
                    item
                    for item in remote_assets
                    if (security.isin and item.get("isin") == security.isin)
                    or (
                        not security.isin
                        and item.get("ticker") == security.ticker
                    )
                    or item.get("name") == security.name
                ),
                None,
            )
            if remote is None:
                remote = await client.create_asset(payload)
                remote_assets.append(remote)
                created += 1
                continue
            asset_id = str(remote["id"])
            await client.update_asset(
                asset_id,
                {
                    key: payload[key]
                    for key in ("name", "units", "purchase_price", "ticker")
                },
            )
            if holding.market_value is not None:
                await client.add_asset_value(
                    asset_id,
                    amount=str(holding.market_value),
                    price=(
                        str(holding.price)
                        if holding.price is not None
                        else None
                    ),
                    observed_at=holding.observed_at.date().isoformat(),
                )
            updated += 1
        return created, updated

    @staticmethod
    def _asset_payload(holding: Holding, security: Security) -> dict[str, Any]:
        return {
            "name": security.name,
            "type": "investment",
            "currency": holding.currency_code,
            "units": str(holding.quantity),
            "valuation_method": "manual",
            "purchase_price": (
                str(holding.cost_basis)
                if holding.cost_basis is not None
                else None
            ),
            "current_value": (
                str(holding.market_value)
                if holding.market_value is not None
                else None
            ),
            "ticker": security.ticker,
            "ticker_exchange": None,
            "isin": security.isin,
            "external_id": security.isin or security.ticker or str(security.id),
            "source": "finance-sync",
        }

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
        holdings_attempted = holdings_imported = holdings_skipped = 0
        try:
            async with self.session_factory() as session:
                client: SecuroClient | _NullClient = (
                    SecuroClient(self.config) if push else _NullClient()
                )
                async with client:
                    push_client = cast(SecuroClient, client)
                    remote_accounts: list[dict[str, Any]] = []
                    remote_assets: list[dict[str, Any]] = []
                    if push:
                        await push_client.login()
                        remote_accounts = await push_client.accounts()
                        remote_assets = await push_client.assets()
                    for account in accounts:
                        txns = await self._transactions(
                            session, account.id, since
                        )
                        attempted += len(txns)
                        positions = await self._holdings(
                            session, account.id, since
                        )
                        holdings_attempted += len(positions)
                        content: bytes | None = None
                        path: Path | None = None
                        if txns:
                            content = self.csv_bytes(txns)
                            filename = (
                                f"securo_{account.id}_"
                                f"{since.date().isoformat()}.csv"
                            )
                            path = destination / filename
                            await asyncio.to_thread(path.write_bytes, content)
                            files.append(path)
                        if push and txns:
                            assert content is not None
                            assert path is not None
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
                        if push and positions:
                            created, updated = await self._push_holdings(
                                push_client, positions, remote_assets
                            )
                            holdings_imported += created
                            holdings_skipped += updated
            return SecuroExportResult(
                status="completed",
                files=files,
                attempted=attempted,
                imported=imported,
                skipped=skipped,
                holdings_attempted=holdings_attempted,
                holdings_imported=holdings_imported,
                holdings_skipped=holdings_skipped,
            )
        except Exception as exc:
            return SecuroExportResult(
                status="failed",
                files=files,
                attempted=attempted,
                imported=imported,
                skipped=skipped,
                holdings_attempted=holdings_attempted,
                holdings_imported=holdings_imported,
                holdings_skipped=holdings_skipped,
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
