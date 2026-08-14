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

import contextlib
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.models import WealthfolioDelivery
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    map_holdings_to_csv,
    map_transaction_to_wf_row,
    map_transactions_to_csv,
)
from finance_sync.models import Account, Holding, Security, Transaction
from finance_sync.observability.metrics import export_runs_total

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
    )

    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
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

    @staticmethod
    def _record_export_metrics(
        result: WealthfolioExportResult,
    ) -> None:
        """Record the export run outcome as a Prometheus counter."""
        export_runs_total.labels(
            exporter="wealthfolio",
            status=result.status,
        ).inc()

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
                exporter_type="wealthfolio",
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
                result = WealthfolioExportResult(
                    status="completed",
                    duration_s=(datetime.now(UTC) - start_ts).total_seconds(),
                )
                self._record_export_metrics(result)
                return result

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
            result = WealthfolioExportResult(
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
            self._record_export_metrics(result)
            return result

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
            result = WealthfolioExportResult(
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
            self._record_export_metrics(result)
            return result

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

    # ── Delivery cursor (idempotent push resume) ────────────────────

    async def _delivery_since(
        self,
        *,
        account_id: str,
        fallback: datetime,
    ) -> datetime:
        """Return the resume timestamp for *account_id*.

        Uses the per-account ``WealthfolioDelivery`` cursor when one
        exists (the timestamp of the last successfully pushed
        transaction); otherwise falls back to *fallback* (e.g. the
        last completed run / 90-day default).
        """
        delivery = await self._get_wealthfolio_delivery(account_id=account_id)
        if delivery is not None and delivery.last_exported_at is not None:
            return delivery.last_exported_at
        return fallback

    async def _get_wealthfolio_delivery(
        self,
        *,
        account_id: str,
    ) -> WealthfolioDelivery | None:
        """Retrieve the WealthfolioDelivery cursor for *account_id*."""
        async with self._session_factory() as session:
            stmt = select(WealthfolioDelivery).where(
                WealthfolioDelivery.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                WealthfolioDelivery.account_id == account_id,  # type: ignore[attr-defined]
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _update_wealthfolio_delivery(
        self,
        *,
        account_id: str,
        transactions: list[Transaction],
        export_run_id: str | None = None,
    ) -> None:
        """Update the WealthfolioDelivery cursor for *account_id*.

        Records the last pushed transaction (id + ``occurred_at``) so a
        subsequent push resumes from that point.  Only called after the
        push for the account succeeded — a failed account keeps its old
        cursor and is re-processed on retry.
        """
        if not transactions:
            return

        last = transactions[-1]
        async with self._session_factory() as session:
            stmt = select(WealthfolioDelivery).where(
                WealthfolioDelivery.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                WealthfolioDelivery.account_id == account_id,  # type: ignore[attr-defined]
            )
            result = await session.execute(stmt)
            delivery = result.scalar_one_or_none()

            if delivery is None:
                delivery = WealthfolioDelivery(
                    tenant_id=self._tenant_id,
                    account_id=account_id,
                    last_exported_transaction_id=last.id,
                    last_exported_at=last.occurred_at,
                    export_run_id=export_run_id,
                )
                session.add(delivery)
            else:
                delivery.last_exported_transaction_id = last.id
                delivery.last_exported_at = last.occurred_at
                if export_run_id is not None:
                    delivery.export_run_id = export_run_id

            await session.flush()

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

    # ── Push to Wealthfolio instance ───────────────────────────────

    async def push_to_wealthfolio(
        self,
        wf_client: WealthfolioClient,
        *,
        accounts: list[Account] | None = None,
        since: datetime | None = None,
        max_transactions: int | None = None,
    ) -> dict[str, Any]:
        """Push exported data directly to a running Wealthfolio instance.

        Authenticates, fetches pending transactions, maps them to
        Wealthfolio format, and imports them via the Wealthfolio API.

        The push is **idempotent**: a per-account delivery cursor
        (``WealthfolioDelivery``) records the last successfully pushed
        transaction, so a subsequent push — or a retry after a partial
        failure — resumes from the cursor instead of re-pushing
        already-delivered transactions.  Failed accounts are recorded
        (and the run marked failed) but do not abort the remaining
        accounts, so a retry only re-processes the accounts that failed.

        Args:
            wf_client: Authenticated :class:`WealthfolioClient`.
            accounts: Optional list of accounts to push. If omitted,
                      loads all active accounts.
            since:    Only push transactions on or after this time.
                      Per-account delivery cursors take precedence
                      over this fallback.
            max_transactions: Hard limit on transactions to push.

        Returns:
            Import result dict with ``imported`` / ``skipped`` /
            ``failed`` counts, the ``run_id`` of the ExportRun that
            tracked this push, and ``errors`` (per-account failure
            details when any account failed).

        Raises:
            WealthfolioAuthError: If the client is not authenticated.
            WealthfolioAPIError:  If the Wealthfolio API rejects data
                                  for every account (fatal failures).
        """
        start_ts = datetime.now(UTC)
        run: ExportRun | None = None
        txns_attempted = 0
        txns_imported = 0
        txns_skipped = 0
        txns_failed = 0
        errors: list[dict[str, str]] = []

        # ── Create ExportRun ──────────────────────────────────────
        async with self._session_factory() as session:
            run = ExportRun(
                status="running",
                started_at=start_ts,
                exporter_type="wealthfolio",
            )
            session.add(run)
            await session.flush()
            log = self._log.bind(export_run_id=str(run.id))

        try:
            _since = since or await self._last_export_time()
            fs_accounts = accounts or await self._load_accounts(None)
            security_map = await self._load_securities()

            for fs_acct in fs_accounts:
                txns: list[Transaction] = []
                try:
                    # Resume from the per-account delivery cursor when
                    # one exists (idempotent resume after partial failure).
                    effective_since = await self._delivery_since(
                        account_id=fs_acct.id,
                        fallback=_since,
                    )
                    txns = await self._fetch_pending_transactions(
                        account_id=fs_acct.id,
                        since=effective_since,
                    )
                    if not txns:
                        continue

                    if max_transactions:
                        txns = txns[:max_transactions]

                    # Map to Wealthfolio API format
                    wf_activities = []
                    for txn in txns:
                        sec = (
                            security_map.get(txn.security_id)
                            if txn.security_id
                            else None
                        )  # type: ignore[arg-type]
                        row = map_transaction_to_wf_row(
                            txn,
                            security=sec,
                            instrument_type_map=self._wf_config.instrument_type_overrides,
                            default_currency=self._wf_config.default_currency,
                        )
                        wf_activities.append(_wf_row_to_api_activity(row))

                    if not wf_activities:
                        continue

                    txns_attempted += len(txns)

                    # Push via Wealthfolio API
                    result = await wf_client.push_activities(wf_activities)
                    imported = result.get("imported", 0)
                    skipped = result.get("skipped", 0)
                    failed = result.get("failed", 0)
                    txns_imported += imported
                    txns_skipped += skipped
                    txns_failed += failed

                    if failed > 0:
                        # The API rejected part of this account's batch —
                        # do NOT advance the cursor so a retry re-pushes
                        # the failed activities (no silent data loss).
                        errors.append(
                            {
                                "account_id": fs_acct.id,
                                "account_name": fs_acct.name,
                                "error": (
                                    f"Wealthfolio API rejected {failed} of "
                                    f"{len(txns)} activities"
                                ),
                            }
                        )
                        log.warning(
                            "wealthfolio_push_partial_rejected",
                            account=fs_acct.name,
                            imported=imported,
                            skipped=skipped,
                            failed=failed,
                        )
                        continue

                    # Advance the delivery cursor only after the push
                    # for this account succeeded (idempotent resume).
                    await self._update_wealthfolio_delivery(
                        account_id=fs_acct.id,
                        transactions=txns,
                        export_run_id=str(run.id),
                    )

                    log.info(
                        "pushed_to_wealthfolio",
                        account=fs_acct.name,
                        imported=imported,
                        skipped=skipped,
                        failed=failed,
                    )
                except Exception as exc:
                    # Record the failure and continue with the next
                    # account so a retry only re-processes what failed.
                    txns_failed += len(txns)
                    errors.append(
                        {
                            "account_id": fs_acct.id,
                            "account_name": fs_acct.name,
                            "error": str(exc)[:512],
                        }
                    )
                    log.warning(
                        "wealthfolio_push_account_failed",
                        account=fs_acct.name,
                        error=str(exc),
                    )

            # ── Complete the run ──────────────────────────────────
            if errors:
                status = "failed"
                error_message = (
                    f"{len(errors)} account(s) failed to push: "
                    + "; ".join(
                        f"{e['account_name']}: {e['error']}" for e in errors[:5]
                    )
                )[:2048]
            else:
                status = "completed"
                error_message = None

            await self._complete_run(
                run,
                status=status,
                attempted=txns_attempted,
                exported=txns_imported,
                _skipped=txns_skipped,
                failed=txns_failed,
                error_message=error_message,
            )
            log.info(
                "wealthfolio_push_completed",
                status=status,
                attempted=txns_attempted,
                imported=txns_imported,
                skipped=txns_skipped,
                failed=txns_failed,
                errors=len(errors),
            )
            return {
                "imported": txns_imported,
                "skipped": txns_skipped,
                "failed": txns_failed,
                "run_id": str(run.id),
                "errors": errors,
            }
        except Exception:
            tb = traceback.format_exc()
            await self._complete_run(
                run,
                status="failed",
                error_message=tb[:2048],
                attempted=txns_attempted,
                exported=txns_imported,
                _skipped=txns_skipped,
                failed=txns_failed,
            )
            self._log.error(
                "wealthfolio_push_failed",
                traceback=tb,
            )
            raise


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _default_since() -> datetime:
    """Return 90 days before now (UTC)."""
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=90)


def _wf_row_to_api_activity(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a Wealthfolio CSV row dict to API activity format.

    The transaction mapper produces CSV-row dicts with string-formatted
    values.  This helper converts them to the types the Wealthfolio
    REST API expects (numbers as floats, booleans as needed).
    """
    activity: dict[str, Any] = {
        "activityType": row.get("activityType", ""),
        "date": row.get("date", ""),
    }

    # Symbol — blank for cash activities
    symbol = row.get("symbol", "")
    if symbol:
        activity["symbol"] = symbol

    # Numeric fields
    for numeric_key in ("quantity", "unitPrice", "amount", "fee", "fxRate"):
        val = row.get(numeric_key, "")
        if val != "" and val is not None:
            with contextlib.suppress(ValueError, TypeError):
                activity[numeric_key] = float(val)

    # String fields
    for str_key in ("currency", "comment", "instrumentType"):
        val = row.get(str_key, "")
        if val:
            activity[str_key] = val

    return activity
