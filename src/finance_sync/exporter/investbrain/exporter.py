"""End-to-end finance-sync to InvestBrain export orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from finance_sync.exporter.investbrain.transaction_mapper import (
    map_transaction_to_investbrain,
)
from finance_sync.exporter.models import ExportRun
from finance_sync.models import Account, Security, Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.exporter.investbrain.client import InvestBrainClient
    from finance_sync.exporter.investbrain.config import InvestBrainConfig


class InvestBrainExporter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: InvestBrainConfig,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._tenant_id = tenant_id

    async def run_export(
        self,
        client: InvestBrainClient,
        *,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
        max_transactions: int | None = None,
    ) -> dict[str, Any]:
        since = since or datetime.now(UTC) - timedelta(days=90)
        async with self._session_factory() as session:
            account_stmt = select(Account).where(
                Account.tenant_id == self._tenant_id,
                Account.is_active.is_(True),
                Account.account_type.in_(["brokerage", "investment"]),
            )
            if account_ids:
                account_stmt = account_stmt.where(Account.id.in_(account_ids))
            accounts = list(
                (await session.execute(account_stmt)).scalars().all()
            )
            account_map = {a.id: a for a in accounts}
            stmt = select(Transaction).where(
                Transaction.tenant_id == self._tenant_id,
                Transaction.account_id.in_(list(account_map)),
                Transaction.transaction_type.in_(["purchase", "sale"]),
                Transaction.occurred_at >= since,
            )
            if not self._config.include_pending:
                stmt = stmt.where(Transaction.status == "booked")
            txns = list(
                (
                    await session.execute(
                        stmt.order_by(Transaction.occurred_at, Transaction.id)
                    )
                )
                .scalars()
                .all()
            )
            if max_transactions:
                txns = txns[:max_transactions]
            securities = {
                s.id: s
                for s in (await session.execute(select(Security)))
                .scalars()
                .all()
            }

        portfolios = await client.list_portfolios()
        portfolio_by_account: dict[str, str] = {}
        for account in accounts:
            marker = f"finance-sync-account:{account.id}"
            match = next(
                (p for p in portfolios if marker in str(p.get("notes", ""))),
                None,
            )
            payload = {
                "title": (
                    f"{self._config.portfolio_name_prefix}: {account.name}"
                ),
                "notes": marker,
                "wishlist": False,
            }
            if match and match.get("id"):
                portfolio_by_account[account.id] = str(match["id"])
                await client.update_portfolio(str(match["id"]), payload)
            else:
                created = await client.create_portfolio(payload)
                portfolio_by_account[account.id] = str(
                    created.get("id") or created.get("data", {}).get("id")
                )

        existing = await client.list_transactions()
        created = skipped = failed = unsupported = 0
        failures: list[str] = []
        for txn in txns:
            payload = map_transaction_to_investbrain(
                txn,
                portfolio_id=portfolio_by_account[txn.account_id],
                security=securities.get(txn.security_id),
            )
            if payload is None:
                unsupported += 1
                continue
            try:
                outcome = await client.upsert_transaction(payload, existing)
                if outcome == "created":
                    created += 1
                    existing.append(payload)
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                failures.append(str(exc))
        status = "completed" if failed == 0 else "failed"
        async with self._session_factory() as session:
            session.add(
                ExportRun(
                    exporter_type="investbrain",
                    status=status,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                    transactions_attempted=len(txns),
                    transactions_exported=created,
                    transactions_failed=failed,
                    error_message=str(failures[:1]) if failures else None,
                )
            )
            await session.commit()
        return {
            "status": status,
            "portfolios": len(portfolio_by_account),
            "transactions_attempted": len(txns),
            "transactions_exported": created,
            "transactions_skipped": skipped,
            "transactions_unsupported": unsupported,
            "transactions_failed": failed,
            "failures": failures,
        }
