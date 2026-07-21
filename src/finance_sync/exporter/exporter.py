"""Actual Budget exporter — orchestration service.

The ``ActualBudgetExporter`` drives an end-to-end export cycle:

    1. Create ``ExportRun`` record (state=running).
    2. Connect to the Actual Budget server via ``ActualBudgetClient``.
    3. For each finance-sync account with pending transactions:
       a. Resolve / create the corresponding AB account.
       b. Fetch new/changed transactions since the last export.
       c. Map them to AB format and import via ``reconcile_transaction``.
    4. Complete the ``ExportRun`` (state=completed / failed).
    5. Disconnect from AB.

Usage::

    exporter = ActualBudgetExporter(
        session_factory=container.session_factory,
        ab_config=ActualBudgetConfig.from_settings(settings),
        tenant_id="...",
    )
    result = await exporter.run_export(
        sync=datetime(2025, 1, 1),
    )
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from finance_sync.exporter.client import (
    ActualBudgetClient,
    ActualBudgetConnectionError,
)

if TYPE_CHECKING:
    from finance_sync.exporter.config import ActualBudgetConfig

from finance_sync.exporter.models import (
    ActualBudgetAccountMapping,
    ExportRun,
)
from finance_sync.exporter.transaction_mapper import (
    map_transaction,
    map_transaction_to_csv_row,
)
from finance_sync.models import Account, Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
    )


logger = structlog.get_logger("finance_sync.exporter")


# ═══════════════════════════════════════════════════════════════════════
# Result type
# ═══════════════════════════════════════════════════════════════════════


class ExportResult:
    """Outcome of a single export run."""

    __slots__ = (
        "accounts_mapped",
        "duration_s",
        "error_message",
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
    ) -> None:
        self.status = status
        self.accounts_mapped = accounts_mapped
        self.transactions_attempted = transactions_attempted
        self.transactions_exported = transactions_exported
        self.transactions_failed = transactions_failed
        self.error_message = error_message
        self.duration_s = duration_s

    def __repr__(self) -> str:
        return (
            f"<ExportResult status={self.status!r} "
            f"txns={self.transactions_exported}/{self.transactions_attempted} "
            f"err={self.error_message!r}>"
        )


# ═══════════════════════════════════════════════════════════════════════
# Exporter service
# ═══════════════════════════════════════════════════════════════════════


class ActualBudgetExporter:
    """Orchestrate a full export cycle to Actual Budget.

    Thread-safe: yes (all AB client I/O runs via ``asyncio.to_thread``).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ab_config: ActualBudgetConfig,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._ab_config = ab_config
        self._tenant_id = tenant_id
        self._log = logger.bind(tenant_id=tenant_id)

    # ── Public API ───────────────────────────────────────────────────

    async def run_export(
        self,
        *,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
        max_transactions: int | None = None,
    ) -> ExportResult:
        """Execute a full export cycle.

        Args:
            since:            Only export transactions on or after this time.
                              Defaults to the timestamp of the last successful
                              export run, or 90 days ago if none exists.
            account_ids:      If provided, only export transactions for these
                              finance-sync account IDs.  When omitted, all
                              active accounts with pending transactions are
                              exported.
            max_transactions: Hard limit on the number of transactions to
                              export in this run (for testing / throttling).

        Returns:
            An ``ExportResult`` named-tuple-like object.
        """
        log = self._log.bind(
            since=(since or _default_since()).isoformat(),
            account_limit=len(account_ids) if account_ids else "all",
        )
        log.info("export_starting")

        start_ts = datetime.now(UTC)
        run: ExportRun | None = None
        txns_attempted = 0
        txns_exported = 0
        txns_failed = 0
        accts_mapped = 0
        _since = since or await self._last_export_time()

        # ── Create ExportRun ──────────────────────────────────────
        async with self._session_factory() as session:
            run = ExportRun(
                status="running",
                started_at=start_ts,
            )
            session.add(run)
            await session.flush()
            log = log.bind(export_run_id=str(run.id))

        try:
            # ── Connect to AB ─────────────────────────────────────
            client = ActualBudgetClient(self._ab_config)
            async with client:
                log.info("ab_connected")

                # ── Resolve account mappings ──────────────────────
                fs_accounts = await self._load_accounts(account_ids)
                log.info("accounts_resolved", count=len(fs_accounts))

                for fs_acct in fs_accounts:
                    # Map or create AB account
                    ab_acct = await self._resolve_ab_account(
                        session, fs_acct, client
                    )
                    if ab_acct is None:
                        log.warning(
                            "ab_account_skip",
                            fs_account_id=fs_acct.id,
                            fs_account_name=fs_acct.name,
                        )
                        continue
                    accts_mapped += 1

                    # Fetch pending transactions
                    txns = await self._fetch_pending_transactions(
                        session,
                        account_id=fs_acct.id,
                        since=_since,
                    )
                    if not txns:
                        log.debug(
                            "no_pending_transactions",
                            account=fs_acct.name,
                        )
                        continue

                    log.info(
                        "exporting_transactions",
                        account=fs_acct.name,
                        count=len(txns),
                    )

                    # Map to AB format
                    mapped = [
                        map_transaction(
                            t,
                            ab_account_name=ab_acct["name"],
                        )
                        for t in txns
                    ]

                    if max_transactions:
                        mapped = mapped[:max_transactions]

                    txns_attempted += len(mapped)

                    # Import into AB using reconcile (dedup-aware)
                    batch_ok = await client.import_transactions_batch(
                        account=ab_acct["name"],
                        transactions=mapped,
                    )
                    txns_exported += batch_ok
                    txns_failed += len(mapped) - batch_ok

                    # Mark exported transactions
                    await self._mark_exported(
                        session, [t.id for t in txns[: len(mapped)]]
                    )

                # ── Also produce a CSV summary for manual import ──
                csv_path = await self._write_csv(
                    session,
                    account_ids=account_ids or [a.id for a in fs_accounts],
                    since=_since,
                )
                if csv_path:
                    log.info("csv_exported", path=str(csv_path))

            # ── Complete the run ─────────────────────────────────
            end_ts = datetime.now(UTC)
            await self._complete_run(
                run,
                status="completed",
                attempted=txns_attempted,
                exported=txns_exported,
                failed=txns_failed,
            )
            log.info(
                "export_completed",
                txns_attempted=txns_attempted,
                txns_exported=txns_exported,
                txns_failed=txns_failed,
                duration_s=(end_ts - start_ts).total_seconds(),
            )
            return ExportResult(
                status="completed",
                accounts_mapped=accts_mapped,
                transactions_attempted=txns_attempted,
                transactions_exported=txns_exported,
                transactions_failed=txns_failed,
                duration_s=(end_ts - start_ts).total_seconds(),
            )

        except ActualBudgetConnectionError as exc:
            end_ts = datetime.now(UTC)
            await self._complete_run(
                run,
                status="failed",
                error_message=str(exc),
                attempted=txns_attempted,
                exported=txns_exported,
                failed=txns_failed,
            )
            self._log.error("export_connection_failed", error=str(exc))
            return ExportResult(
                status="failed",
                accounts_mapped=accts_mapped,
                transactions_attempted=txns_attempted,
                transactions_exported=txns_exported,
                transactions_failed=txns_failed,
                error_message=str(exc),
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        except Exception:
            end_ts = datetime.now(UTC)
            tb = traceback.format_exc()
            await self._complete_run(
                run,
                status="failed",
                error_message=tb[:2048],
                attempted=txns_attempted,
                exported=txns_exported,
                failed=txns_failed,
            )
            self._log.error("export_failed", traceback=tb)
            return ExportResult(
                status="failed",
                accounts_mapped=accts_mapped,
                transactions_attempted=txns_attempted,
                transactions_exported=txns_exported,
                transactions_failed=txns_failed,
                error_message=tb[:2048],
                duration_s=(end_ts - start_ts).total_seconds(),
            )

    # ── Account resolution ──────────────────────────────────────────

    async def _resolve_ab_account(
        self,
        session: AsyncSession,
        fs_acct: Account,
        client: ActualBudgetClient,
    ) -> dict[str, Any] | None:
        """Find an AB account for *fs_acct*, creating one if needed.

        Checks:
        1. ``ActualBudgetAccountMapping`` table for an existing mapping.
        2. Name match: finance-sync account name (or override) in AB.
        3. Creates a new AB account with the mapped name.

        Returns the AB account dict or ``None`` if resolution failed.
        """
        # 1. Check persisted mapping
        stmt = select(ActualBudgetAccountMapping).where(
            ActualBudgetAccountMapping.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
            ActualBudgetAccountMapping.account_id == fs_acct.id,  # type: ignore[attr-defined]
        )
        result = await session.execute(stmt)
        mapping: ActualBudgetAccountMapping | None = result.scalar_one_or_none()

        if mapping is not None:
            ab_acct = await client.get_account_by_name(mapping.ab_account_name)
            if ab_acct is not None:
                return ab_acct
            # Mapping exists but the AB account was deleted — fall through
            # to re-create

        # 2. Determine the AB account name
        ab_name = self._ab_config.account_name_overrides.get(
            fs_acct.id, fs_acct.name
        )

        # 3. Find or create in AB
        ab_acct = await client.get_or_create_account(
            ab_name,
            off_budget=self._ab_config.default_off_budget,
        )

        # 4. Persist the mapping
        new_mapping = ActualBudgetAccountMapping(
            tenant_id=self._tenant_id,
            account_id=fs_acct.id,
            ab_account_id=ab_acct["id"],
            ab_account_name=ab_acct["name"],
        )
        session.add(new_mapping)
        await session.flush()

        return ab_acct

    # ── Transaction queries ─────────────────────────────────────────

    async def _load_accounts(
        self,
        account_ids: list[str] | None,
    ) -> list[Account]:
        """Load finance-sync accounts, optionally filtered."""
        async with self._session_factory() as session:
            stmt = select(Account).where(
                Account.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                Account.is_active.is_(True),  # type: ignore[attr-defined]
            )
            if account_ids:
                stmt = stmt.where(
                    Account.id.in_(account_ids)  # type: ignore[attr-defined]
                )
            stmt = stmt.order_by(Account.name)  # type: ignore[attr-defined]
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _fetch_pending_transactions(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        since: datetime,
    ) -> list[Transaction]:
        """Fetch transactions for *account_id* that haven't been exported."""
        stmt = (
            select(Transaction)
            .where(
                Transaction.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                Transaction.account_id == account_id,  # type: ignore[attr-defined]
                Transaction.occurred_at >= since,  # type: ignore[attr-defined]
                Transaction.status.in_(["booked", "pending"]),  # type: ignore[attr-defined]
            )
            .order_by(Transaction.occurred_at)  # type: ignore[attr-defined]
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _last_export_time(self) -> datetime:
        """Return the timestamp of the last successful export.

        Defaults to 90 days ago if no previous export exists.
        """
        async with self._session_factory() as session:
            stmt = (
                select(ExportRun.started_at)
                .where(ExportRun.status == "completed")  # type: ignore[attr-defined]
                .order_by(ExportRun.started_at.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row
        return _default_since()

    async def _mark_exported(
        self,
        session: AsyncSession,
        transaction_ids: list[str],
    ) -> None:
        """Mark transactions as exported (no-op for now, reserved for
        future use).

        In a future iteration this could set an ``exported_at`` timestamp
        on each transaction or write to a join table.  Currently the
        export is idempotent via AB's ``imported_id`` dedup mechanism, so
        explicit marking is optional.
        """
        # Placeholder — dedup is handled by AB's imported_id mechanism
        _ = session
        _ = transaction_ids  # noqa: RUF100 (placeholder)

    async def _write_csv(
        self,
        session: AsyncSession,
        *,
        account_ids: list[str],
        since: datetime,
    ) -> str | None:
        """Write a CSV file with pending transactions for manual import.

        Returns the file path or ``None`` if no transactions were found.
        """
        import csv
        import io
        import os
        import tempfile

        txns = await self._fetch_pending_transactions_for_csv(
            session, account_ids, since
        )
        if not txns:
            return None

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["Date", "Payee", "Category", "Notes", "Amount"],
        )
        writer.writeheader()
        for t in txns:
            writer.writerow(map_transaction_to_csv_row(t))

        content = buf.getvalue()
        if not content.strip():
            return None

        fd, path = tempfile.mkstemp(
            prefix="ab_export_", suffix=".csv", dir="/tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            return path
        except OSError:
            return None

    async def _fetch_pending_transactions_for_csv(
        self,
        session: AsyncSession,
        account_ids: list[str],
        since: datetime,
    ) -> list[Transaction]:
        """Fetch pending transactions for all *account_ids*."""
        stmt = (
            select(Transaction)
            .where(
                Transaction.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                Transaction.account_id.in_(account_ids),  # type: ignore[attr-defined]
                Transaction.occurred_at >= since,  # type: ignore[attr-defined]
                Transaction.status.in_(["booked", "pending"]),  # type: ignore[attr-defined]
            )
            .order_by(Transaction.occurred_at)  # type: ignore[attr-defined]
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ── ExportRun management ─────────────────────────────────────────

    async def _complete_run(
        self,
        run: ExportRun | None,
        *,
        status: str,
        attempted: int = 0,
        exported: int = 0,
        failed: int = 0,
        error_message: str | None = None,
    ) -> None:
        """Update the ExportRun record with final status."""
        if run is None:
            return
        async with self._session_factory() as session:
            run = await session.merge(run)
            run.status = status
            run.completed_at = datetime.now(UTC)
            run.transactions_attempted = attempted
            run.transactions_exported = exported
            run.transactions_failed = failed
            if error_message is not None:
                run.error_message = error_message
            await session.flush()


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _default_since() -> datetime:
    """Return 90 days before now (UTC)."""
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=90)
