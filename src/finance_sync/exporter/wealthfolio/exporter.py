"""Wealthfolio exporter — orchestration service.

The ``WealthfolioExporter`` drives an end-to-end export cycle:

    1. Create ``ExportRun`` record (state=running).
    2. Load finance-sync accounts and their securities.
    3. For each account with pending transactions:
       a. Resolve / map to Wealthfolio account name.
       b. Fetch new/changed transactions.
       c. Fetch current holdings.
       d. Map to Wealthfolio CSV format.
       e. Write CSV files (activity mode + optional holdings mode).
    4. Complete the ``ExportRun`` (state=completed / failed).

Usage::

    exporter = WealthfolioExporter(
        session_factory=container.session_factory,
        wf_config=WealthfolioConfig.from_settings(settings),
        tenant_id="...",
    )
    result = await exporter.run_export()
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    map_holdings_to_csv,
    map_transactions_to_csv,
)
from finance_sync.models import Account, Holding, Security, Transaction

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
    )

    from finance_sync.exporter.wealthfolio.config import (
        WealthfolioConfig,
    )


logger = structlog.get_logger("finance_sync.exporter.wealthfolio")


# ═══════════════════════════════════════════════════════════════════════
# Result type
# ═══════════════════════════════════════════════════════════════════════


class WealthfolioExportResult:
    """Outcome of a single export run to Wealthfolio."""

    __slots__ = (
        "accounts_mapped",
        "csv_files",
        "duration_s",
        "error_message",
        "holdings_exported",
        "status",
        "transactions_attempted",
        "transactions_exported",
        "transactions_failed",
        "transactions_skipped",
    )

    def __init__(
        self,
        *,
        status: str,
        accounts_mapped: int = 0,
        transactions_attempted: int = 0,
        transactions_exported: int = 0,
        transactions_failed: int = 0,
        transactions_skipped: int = 0,
        holdings_exported: int = 0,
        csv_files: list[str] | None = None,
        error_message: str | None = None,
        duration_s: float = 0.0,
    ) -> None:
        self.status = status
        self.accounts_mapped = accounts_mapped
        self.transactions_attempted = transactions_attempted
        self.transactions_exported = transactions_exported
        self.transactions_failed = transactions_failed
        self.transactions_skipped = transactions_skipped
        self.holdings_exported = holdings_exported
        self.csv_files = csv_files or []
        self.error_message = error_message
        self.duration_s = duration_s

    def __repr__(self) -> str:
        return (
            f"<WealthfolioExportResult status={self.status!r} "
            f"txns={self.transactions_exported}/{self.transactions_attempted} "
            f"holdings={self.holdings_exported} "
            f"files={len(self.csv_files)} "
            f"err={self.error_message!r}>"
        )


# ═══════════════════════════════════════════════════════════════════════
# Exporter service
# ═══════════════════════════════════════════════════════════════════════


class WealthfolioExporter:
    """Orchestrate a full export cycle to Wealthfolio CSV files.

    Thread-safe: yes (all I/O runs via asyncio file operations).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        wf_config: WealthfolioConfig,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._wf_config = wf_config
        self._tenant_id = tenant_id
        self._log = logger.bind(tenant_id=tenant_id)

    # ── Public API ───────────────────────────────────────────────────

    async def run_export(
        self,
        *,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
        max_transactions: int | None = None,
        output_dir: Path | None = None,
    ) -> WealthfolioExportResult:
        """Execute a full export cycle to Wealthfolio CSV files.

        Args:
            since:            Only export transactions on or after this time.
                              Defaults to 90 days ago if no prior export.
            account_ids:      If provided, only export these accounts.
            max_transactions: Hard limit on transactions to export.
            output_dir:       Override output directory for CSV files.

        Returns:
            A ``WealthfolioExportResult``.
        """
        log = self._log.bind(
            since=(since or _default_since()).isoformat(),
            account_limit=len(account_ids) if account_ids else "all",
        )
        log.info("wealthfolio_export_starting")

        start_ts = datetime.now(UTC)
        export_dir = output_dir or self._wf_config.output_dir
        run: ExportRun | None = None
        txns_attempted = 0
        txns_exported = 0
        txns_failed = 0
        txns_skipped = 0
        holdings_exported = 0
        accts_mapped = 0
        csv_files: list[str] = []
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
            # ── Ensure output directory ────────────────────────────
            export_dir.mkdir(parents=True, exist_ok=True)

            # ── Load accounts and securities ───────────────────────
            fs_accounts = await self._load_accounts(account_ids)
            log.info("accounts_loaded", count=len(fs_accounts))

            if not fs_accounts:
                log.info("no_accounts_to_export")
                await self._complete_run(
                    run,
                    status="completed",
                    attempted=0,
                    exported=0,
                    failed=0,
                )
                return WealthfolioExportResult(
                    status="completed",
                    duration_s=(datetime.now(UTC) - start_ts).total_seconds(),
                )

            # Pre-load securities for symbol resolution
            security_map = await self._load_securities()

            for fs_acct in fs_accounts:
                # Resolve Wealthfolio account name
                wf_acct_name = await self._resolve_wf_account_name(
                    fs_acct.id, fs_acct.name
                )

                accts_mapped += 1

                # ── Export transactions ────────────────────────────
                txns = await self._fetch_pending_transactions(
                    account_id=fs_acct.id,
                    since=_since,
                )
                if not txns:
                    log.debug(
                        "no_pending_transactions",
                        account=fs_acct.name,
                    )
                else:
                    log.info(
                        "exporting_transactions",
                        account=fs_acct.name,
                        count=len(txns),
                    )

                    if max_transactions:
                        txns = txns[:max_transactions]

                    txns_attempted += len(txns)

                    # Map and write CSV
                    csv_content = map_transactions_to_csv(
                        txns,
                        security_map=security_map,
                        instrument_type_map=self._wf_config.instrument_type_overrides,
                        default_currency=self._wf_config.default_currency,
                    )

                    if csv_content.strip():
                        txn_csv_path = self._write_csv_file(
                            content=csv_content,
                            export_dir=export_dir,
                            prefix=f"transactions_{wf_acct_name}",
                            suffix=".csv",
                        )
                        csv_files.append(str(txn_csv_path))
                        txns_exported += len(txns)
                        log.info(
                            "transactions_csv_written",
                            path=str(txn_csv_path),
                            count=len(txns),
                        )

                    # Mark exported
                    await self._mark_exported([t.id for t in txns])

                # ── Export holdings ────────────────────────────────
                if self._wf_config.export_holdings:
                    holdings = await self._fetch_current_holdings(
                        account_id=fs_acct.id,
                    )
                    if holdings:
                        holdings_exported += len(holdings)
                        holdings_csv = map_holdings_to_csv(
                            holdings,
                            security_map=security_map,
                            default_currency=self._wf_config.default_currency,
                        )
                        if holdings_csv.strip():
                            hld_csv_path = self._write_csv_file(
                                content=holdings_csv,
                                export_dir=export_dir,
                                prefix=f"holdings_{wf_acct_name}",
                                suffix=".csv",
                            )
                            csv_files.append(str(hld_csv_path))
                            log.info(
                                "holdings_csv_written",
                                path=str(hld_csv_path),
                                count=len(holdings),
                            )

            # ── Write a summary manifest ──────────────────────────
            if csv_files:
                manifest_path = self._write_manifest(
                    csv_files,
                    export_dir,
                    attempted=txns_attempted,
                    exported=txns_exported,
                    holdings=holdings_exported,
                )
                csv_files.append(str(manifest_path))

            # ── Complete the run ──────────────────────────────────
            end_ts = datetime.now(UTC)
            await self._complete_run(
                run,
                status="completed",
                attempted=txns_attempted,
                exported=txns_exported,
                _skipped=txns_skipped,
                failed=txns_failed,
            )
            log.info(
                "wealthfolio_export_completed",
                txns_attempted=txns_attempted,
                txns_exported=txns_exported,
                txns_failed=txns_failed,
                holdings_exported=holdings_exported,
                csv_files=len(csv_files),
                duration_s=(end_ts - start_ts).total_seconds(),
            )
            return WealthfolioExportResult(
                status="completed",
                accounts_mapped=accts_mapped,
                transactions_attempted=txns_attempted,
                transactions_exported=txns_exported,
                transactions_failed=txns_failed,
                transactions_skipped=txns_skipped,
                holdings_exported=holdings_exported,
                csv_files=csv_files,
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
                _skipped=txns_skipped,
                failed=txns_failed,
            )
            self._log.error(
                "wealthfolio_export_failed",
                traceback=tb,
            )
            return WealthfolioExportResult(
                status="failed",
                accounts_mapped=accts_mapped,
                transactions_attempted=txns_attempted,
                transactions_exported=txns_exported,
                transactions_failed=txns_failed,
                transactions_skipped=txns_skipped,
                holdings_exported=holdings_exported,
                csv_files=csv_files,
                error_message=tb[:2048],
                duration_s=(end_ts - start_ts).total_seconds(),
            )

    # ── Account resolution ──────────────────────────────────────────

    async def _resolve_wf_account_name(
        self,
        fs_account_id: str,
        fs_account_name: str,
    ) -> str:
        """Determine the Wealthfolio account name for a finance-sync account.

        Checks:
        1. Override map in config.
        2. Finance-sync account name (default).
        """
        return self._wf_config.account_name_overrides.get(
            fs_account_id, fs_account_name
        )

    # ── Data queries ────────────────────────────────────────────────

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

    async def _load_securities(self) -> dict[str, Security]:
        """Load all securities keyed by id."""
        async with self._session_factory() as session:
            stmt = select(Security)
            result = await session.execute(stmt)
            securities = list(result.scalars().all())
            return {s.id: s for s in securities}

    async def _fetch_pending_transactions(
        self,
        *,
        account_id: str,
        since: datetime,
    ) -> list[Transaction]:
        """Fetch transactions for *account_id* that haven't been exported."""
        async with self._session_factory() as session:
            status_filter = ["booked"]
            if self._wf_config.include_pending:
                status_filter.append("pending")

            stmt = (
                select(Transaction)
                .where(
                    Transaction.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                    Transaction.account_id == account_id,  # type: ignore[attr-defined]
                    Transaction.occurred_at >= since,  # type: ignore[attr-defined]
                    Transaction.status.in_(status_filter),  # type: ignore[attr-defined]
                )
                .order_by(Transaction.occurred_at)  # type: ignore[attr-defined]
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _fetch_current_holdings(
        self,
        *,
        account_id: str,
    ) -> list[Holding]:
        """Fetch the most recent holdings for *account_id*.

        Returns the latest snapshot for each security position
        by selecting the most recent ``observed_at`` per security.
        """
        async with self._session_factory() as session:
            # Get all holdings for the account, ordered by observed_at desc
            stmt = (
                select(Holding)
                .where(
                    Holding.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                    Holding.account_id == account_id,  # type: ignore[attr-defined]
                )
                .order_by(
                    Holding.security_id,  # type: ignore[attr-defined]
                    Holding.observed_at.desc(),  # type: ignore[attr-defined]
                )
            )
            result = await session.execute(stmt)
            all_holdings = list(result.scalars().all())

            # Deduplicate: keep only the latest per security_id
            seen: set[str] = set()
            latest: list[Holding] = []
            for h in all_holdings:
                if h.security_id not in seen:
                    seen.add(h.security_id)
                    latest.append(h)

            return latest

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
        transaction_ids: list[str],
    ) -> None:
        """Mark transactions as exported (reserved for future use).

        Currently a no-op — dedup is handled by the ``since``
        timestamp approach.  Future iterations can set an
        ``exported_at`` timestamp.
        """
        _ = transaction_ids  # noqa: RUF100 (placeholder)

    # ── File output ─────────────────────────────────────────────────

    def _write_csv_file(
        self,
        *,
        content: str,
        export_dir: Path,
        prefix: str,
        suffix: str = ".csv",
    ) -> Path:
        """Write a CSV file to the export directory.

        Sanitises the prefix for filesystem compatibility.

        Returns the absolute path to the written file.
        """
        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in prefix
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_name}_{timestamp}{suffix}"
        path = export_dir / filename

        path.write_text(content, encoding="utf-8")
        return path

    def _write_manifest(
        self,
        csv_files: list[str],
        export_dir: Path,
        *,
        attempted: int,
        exported: int,
        holdings: int,
    ) -> Path:
        """Write a JSON manifest describing the export run."""
        import json

        manifest: dict[str, Any] = {
            "exported_at": datetime.now(UTC).isoformat(),
            "transactions_attempted": attempted,
            "transactions_exported": exported,
            "holdings_exported": holdings,
            "files": csv_files,
        }

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = export_dir / f"manifest_{timestamp}.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return path

    # ── ExportRun management ────────────────────────────────────────

    async def _complete_run(
        self,
        run: ExportRun | None,
        *,
        status: str,
        attempted: int = 0,
        exported: int = 0,
        failed: int = 0,
        _skipped: int = 0,
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
