"""End-to-end Ghostfolio export orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from finance_sync.exporter.ghostfolio.transaction_mapper import (
    map_transaction_to_ghostfolio,
)
from finance_sync.exporter.models import ExportRun
from finance_sync.models import Security, Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.exporter.ghostfolio.client import GhostfolioClient
    from finance_sync.exporter.ghostfolio.config import GhostfolioConfig


class GhostfolioExporter:
    """Read canonical booked transactions and import them into Ghostfolio."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: GhostfolioConfig,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._tenant_id = tenant_id

    async def run_export(
        self,
        client: GhostfolioClient,
        *,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
        max_transactions: int | None = None,
    ) -> dict[str, Any]:
        since = since or datetime.now(UTC) - timedelta(days=90)
        async with self._session_factory() as session:
            status_filter = (
                ["booked", "pending"]
                if self._config.include_pending
                else ["booked"]
            )
            stmt = (
                select(Transaction)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.status.in_(status_filter),
                    Transaction.occurred_at >= since,
                )
                .order_by(Transaction.occurred_at, Transaction.id)
            )
            if account_ids:
                stmt = stmt.where(Transaction.account_id.in_(account_ids))
            txns = list((await session.execute(stmt)).scalars().all())
            if max_transactions:
                txns = txns[:max_transactions]
            securities = {
                s.id: s
                for s in (await session.execute(select(Security)))
                .scalars()
                .all()
            }
        activities = [
            map_transaction_to_ghostfolio(
                t,
                security=securities.get(t.security_id),
                data_source=self._config.data_source,
            )
            for t in txns
        ]
        result = await client.import_activities(activities)
        status = "completed" if not result["failed"] else "failed"
        async with self._session_factory() as session:
            run = ExportRun(
                exporter_type="ghostfolio",
                status=status,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                transactions_attempted=len(txns),
                transactions_exported=result["imported"],
                transactions_failed=result["failed"],
                error_message=(
                    str(result["failures"][:1]) if result["failures"] else None
                ),
            )
            session.add(run)
            await session.commit()
        return {"status": status, "transactions_attempted": len(txns), **result}
