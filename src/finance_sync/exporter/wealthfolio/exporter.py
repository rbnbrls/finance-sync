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

import asyncio
import contextlib
import inspect
import traceback
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import structlog
from sqlalchemy import and_, func, or_, select

from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.extensions import build_extension_payload
from finance_sync.exporter.wealthfolio.models import (
    WealthfolioAccountMapping,
    WealthfolioDelivery,
)
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    InvalidFxRateError,
    UnresolvedCashCurrencyError,
    UnresolvedSecurityExportError,
    map_holding_to_wf_row,
    map_holdings_to_csv,
    map_security_catalog_to_csv,
    map_tax_lots_to_csv,
    map_transaction_to_wf_row,
    map_transactions_to_csv,
    validate_fx_observation,
)
from finance_sync.models import (
    Account,
    FxRate,
    Holding,
    Security,
    SecurityMetadataObservation,
    SecurityPrice,
    TaxLot,
    Transaction,
)
from finance_sync.observability.glitchtip import capture_connector_exception
from finance_sync.sync.errors import categorize_export_error

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
        "run_id",
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
        run_id: str | None = None,
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
        self.run_id = run_id

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

    capabilities = {
        "accounts": "write",
        "cash_activities": "write",
        "category_assignments": "write",
        "splits": "write",
        "events": "write",
        "notes": "write",
        "attachments": "read",
        "bidirectional": False,
    }

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        wf_config: WealthfolioConfig,
        tenant_id: str,
        target_id: str = "legacy",
    ) -> None:
        self._session_factory = session_factory
        self._wf_config = wf_config
        self._tenant_id = tenant_id
        self._target_id = target_id
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
        extension_transactions: list[Transaction] = []
        _since = since or await self._last_export_time()

        # ── Create ExportRun ──────────────────────────────────────
        async with self._session_factory() as session:
            run = ExportRun(
                tenant_id=self._tenant_id,
                status="running",
                started_at=start_ts,
                exporter_type="wealthfolio",
                target_id=self._target_id,
                account_scope=list(account_ids) if account_ids else None,
            )
            session.add(run)
            await session.flush()
            await session.commit()
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
                    run_id=str(run.id),
                )

            # Pre-load securities for symbol resolution
            security_map = await self._load_securities()

            # Emit every known security, including instruments without an
            # activity in the selected period (for example benchmarks).
            if security_map:
                catalog_path = self._write_csv_file(
                    content=map_security_catalog_to_csv(
                        list(security_map.values())
                    ),
                    export_dir=export_dir,
                    prefix="wealthfolio_asset_catalog",
                )
                csv_files.append(str(catalog_path))
                log.info(
                    "asset_catalog_csv_written",
                    path=str(catalog_path),
                    count=len(security_map),
                )

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

                    extension_transactions.extend(txns)

                    txns_attempted += len(txns)

                    # Map and write CSV
                    csv_content = map_transactions_to_csv(
                        txns,
                        security_map=security_map,
                        instrument_type_map=self._wf_config.instrument_type_overrides,
                        default_currency=self._wf_config.default_currency,
                        account_currency=fs_acct.currency_code,
                        allow_multi_currency_cash=(
                            _supports_multi_currency_cash(fs_acct)
                        ),
                        import_run_id=str(run.id),
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

                tax_lots = await self._fetch_tax_lots(account_id=fs_acct.id)
                if tax_lots:
                    lots_path = self._write_csv_file(
                        content=map_tax_lots_to_csv(
                            tax_lots, security_map=security_map
                        ),
                        export_dir=export_dir,
                        prefix=f"tax_lots_{wf_acct_name}",
                        suffix=".csv",
                    )
                    csv_files.append(str(lots_path))
                    log.info(
                        "tax_lots_csv_written",
                        path=str(lots_path),
                        count=len(tax_lots),
                    )

            # ── Write a summary manifest ──────────────────────────
            extension_accounts = [
                account
                for account in fs_accounts
                if isinstance(account.provider_metadata, dict)
                and any(
                    key in account.provider_metadata
                    for key in (
                        "portfolios",
                        "allocations",
                        "goals",
                        "spending",
                        "net_worth",
                        "alternative_assets",
                    )
                )
            ]
            if extension_accounts or extension_transactions:
                import json

                extension_path = self._write_csv_file(
                    content="",
                    export_dir=export_dir,
                    prefix="wealthfolio_extensions",
                    suffix=".json",
                )
                extension_path.write_text(
                    json.dumps(
                        build_extension_payload(
                            accounts=extension_accounts,
                            transactions=extension_transactions,
                        ),
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )
                csv_files.append(str(extension_path))
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
                run_id=str(run.id),
            )

        except asyncio.CancelledError:
            # A worker shutdown/cancellation must not leave an export looking
            # active forever.  Persist the terminal state before propagating
            # cancellation so health checks and operators can distinguish an
            # interrupted run from an in-flight one.
            await self._complete_run(
                run,
                status="cancelled",
                error_message="Export cancelled",
                attempted=txns_attempted,
                exported=txns_exported,
                _skipped=txns_skipped,
                failed=txns_failed,
            )
            raise
        except Exception as exc:
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
            capture_connector_exception(
                exc,
                connector="wealthfolio",
                operation="export",
                correlation_id=str(run.id),
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
                run_id=str(run.id),
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
        """Load finance-sync accounts, optionally filtered.

        A destination belongs to the application's single owner.  All active
        investment accounts selected for that destination are therefore
        eligible for export; sharing visibility is not an export permission
        boundary.

        The tenant's per-connection account selection is applied too: a
        connection that pinned ``selected_accounts`` only exports the
        accounts in that selection (deselected accounts are never
        exported), while connections without a selection export
        everything they synced.  Legacy rows without a connection scope
        keep exporting (owner-scoped).
        """
        from finance_sync.services.account_selection import (
            account_is_selected,
            load_account_selection,
        )

        async with self._session_factory() as session:
            stmt = select(Account).where(
                Account.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                Account.is_active.is_(True),  # type: ignore[attr-defined]
                Account.account_type.in_(  # type: ignore[attr-defined]
                    ["investment", "brokerage", "checking", "savings", "cash"]
                ),
            )
            if account_ids:
                stmt = stmt.where(  # type: ignore[attr-defined]
                    Account.id.in_(account_ids)  # type: ignore[attr-defined]
                )
            stmt = stmt.order_by(Account.name)  # type: ignore[attr-defined]
            result = await session.execute(stmt)

            # Per-connection account selection (multi-connection support).
            selection = await load_account_selection(session, self._tenant_id)
            return [
                account
                for account in result.scalars().all()
                if account_is_selected(account, selection)
            ]

    async def _load_securities(self) -> dict[str, Security]:
        """Load all securities keyed by id."""
        async with self._session_factory() as session:
            stmt = select(Security)
            result = await session.execute(stmt)
            securities = list(result.scalars().all())
            return {s.id: s for s in securities}

    async def export_asset_catalog(
        self,
        *,
        output_dir: Path | None = None,
    ) -> Path | None:
        """Write the complete connector-owned security catalog.

        This is independent of activity export so securities without
        transactions, including benchmark instruments, are not lost.
        """
        securities = await self._load_securities()
        if not securities:
            return None
        metadata = await self._load_security_metadata(set(securities))
        export_dir = output_dir or self._wf_config.output_dir
        export_dir.mkdir(parents=True, exist_ok=True)
        return self._write_csv_file(
            content=map_security_catalog_to_csv(
                list(securities.values()), metadata=metadata
            ),
            export_dir=export_dir,
            prefix="wealthfolio_asset_catalog",
        )

    async def _load_security_metadata(
        self,
        security_ids: set[str],
    ) -> dict[str, list[SecurityMetadataObservation]]:
        """Load structured metadata used by the connector-owned catalog."""
        if not security_ids:
            return {}
        async with self._session_factory() as session:
            result = await session.execute(
                select(SecurityMetadataObservation)
                .where(
                    SecurityMetadataObservation.security_id.in_(security_ids)
                )
                .order_by(SecurityMetadataObservation.timestamp)
            )
            grouped: dict[str, list[SecurityMetadataObservation]] = {}
            for observation in result.scalars().all():
                grouped.setdefault(observation.security_id, []).append(
                    observation
                )
            return grouped

    async def export_historical_holdings(
        self,
        *,
        account_ids: list[str] | None = None,
        output_dir: Path | None = None,
    ) -> list[Path]:
        """Write all time-versioned holding observations."""
        securities = await self._load_securities()
        accounts = await self._load_accounts(account_ids)
        export_dir = output_dir or self._wf_config.output_dir
        export_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for account in accounts:
            holdings = await self._fetch_historical_holdings(
                account_id=account.id
            )
            if holdings:
                paths.append(
                    self._write_csv_file(
                        content=map_holdings_to_csv(
                            holdings,
                            security_map=securities,
                            default_currency=self._wf_config.default_currency,
                        ),
                        export_dir=export_dir,
                        prefix=f"holdings_history_{account.name}",
                    )
                )
        return paths

    async def _load_security_prices(
        self, security_ids: set[str]
    ) -> list[SecurityPrice]:
        """Load daily prices used for Wealthfolio performance history."""
        if not security_ids:
            return []
        async with self._session_factory() as session:
            stmt = (
                select(SecurityPrice)
                .where(SecurityPrice.security_id.in_(security_ids))
                .where(SecurityPrice.interval == "1d")
                .order_by(SecurityPrice.timestamp)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _load_fx_rates(self) -> list[FxRate]:
        """Load canonical historical FX observations."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(FxRate).order_by(FxRate.timestamp)
            )
            scalar_result = result.scalars()
            if inspect.isawaitable(scalar_result):
                scalar_result = await scalar_result
            rows = scalar_result.all()
            if inspect.isawaitable(rows):
                rows = await rows
            return list(rows)

    async def _sync_historical_holdings(
        self,
        *,
        wf_client: WealthfolioClient,
        fs_account: Account,
        wf_account_id: str,
        security_map: dict[str, Security],
    ) -> list[dict[str, str]]:
        """Import every dated source snapshot during a full sync."""
        if not self._wf_config.export_holdings:
            return []
        holdings = await self._fetch_historical_holdings(
            account_id=fs_account.id
        )
        if len({holding.observed_at.date() for holding in holdings}) <= 1:
            return []
        rows: list[dict[str, Any]] = []
        for holding in holdings:
            try:
                rows.append(
                    map_holding_to_wf_row(
                        holding,
                        security=security_map.get(holding.security_id),
                        default_currency=self._wf_config.default_currency,
                    )
                )
            except UnresolvedSecurityExportError as exc:
                return [
                    {
                        "account_id": fs_account.id,
                        "account_name": fs_account.name,
                        "error": str(exc),
                    }
                ]
        result = await wf_client.import_holdings(rows, wf_account_id)
        if result.get("validationErrors"):
            return [
                {
                    "account_id": fs_account.id,
                    "account_name": fs_account.name,
                    "error": "Wealthfolio wees historische holdings af.",
                }
            ]
        return []

    async def _sync_quote_history(
        self,
        *,
        wf_client: WealthfolioClient,
        security_map: dict[str, Security],
    ) -> int:
        """Project finance-sync daily prices into Wealthfolio."""
        prices = await self._load_security_prices(set(security_map))
        if not prices:
            return 0
        assets = await wf_client.get_assets()
        by_identity: dict[str, str] = {}
        for asset in assets:
            if not asset.get("id"):
                continue
            for value in (
                asset.get("displayCode"),
                asset.get("symbol"),
                asset.get("isin"),
            ):
                if value:
                    by_identity[str(value).upper()] = str(asset["id"])

        synced = 0
        for price in prices:
            security = security_map.get(price.security_id)
            close = price.price_close
            if security is None or close is None:
                continue
            asset_id = next(
                (
                    by_identity.get(str(value).upper())
                    for value in (security.ticker, security.isin)
                    if value and by_identity.get(str(value).upper())
                ),
                None,
            )
            if asset_id is None:
                continue
            timestamp = price.timestamp.astimezone(UTC).isoformat()
            await wf_client.upsert_quote(
                asset_id,
                {
                    "id": f"{asset_id}_{price.timestamp.date()}_FINANCE_SYNC",
                    "createdAt": datetime.now(UTC).isoformat(),
                    "source": "FINANCE_SYNC",
                    "timestamp": timestamp,
                    "assetId": asset_id,
                    "open": str(price.price_open or close),
                    "high": str(price.price_high or close),
                    "low": str(price.price_low or close),
                    "volume": str(price.volume or 0),
                    "close": str(close),
                    "adjclose": str(close),
                    "currency": price.currency_code,
                },
            )
            synced += 1
        return synced

    async def _sync_fx_history(self, wf_client: WealthfolioClient) -> int:
        """Project canonical FX observations into Wealthfolio FX assets."""
        rates = await self._load_fx_rates()
        if not rates:
            return 0

        assets = await wf_client.get_assets()
        asset_ids: dict[tuple[str, str], str] = {}
        for asset in assets:
            if not asset.get("id"):
                continue
            from_currency = asset.get("instrumentSymbol")
            to_currency = asset.get("quoteCcy")
            if from_currency and to_currency:
                asset_ids[
                    (str(from_currency).upper(), str(to_currency).upper())
                ] = str(asset["id"])

        # Create missing pairs once, using the latest canonical rate. The
        # returned pair id is also the asset id used by the quote endpoint.
        grouped: dict[tuple[str, str], list[FxRate]] = {}
        for rate in rates:
            try:
                validate_fx_observation(
                    base_currency=rate.base_currency,
                    quote_currency=rate.quote_currency,
                    rate=Decimal(rate.rate),
                )
            except InvalidFxRateError as exc:
                message = (
                    f"Ongeldige FX-observatie {rate.base_currency}/"
                    f"{rate.quote_currency}: {exc}"
                )
                raise ValueError(message) from exc
            grouped.setdefault(
                (rate.base_currency.upper(), rate.quote_currency.upper()), []
            ).append(rate)
        for pair, pair_rates in grouped.items():
            if pair not in asset_ids:
                created = await wf_client.add_exchange_rate(
                    from_currency=pair[0],
                    to_currency=pair[1],
                    rate=str(pair_rates[-1].rate),
                )
                if created.get("id"):
                    asset_ids[pair] = str(created["id"])

        synced = 0
        for pair, pair_rates in grouped.items():
            asset_id = asset_ids.get(pair)
            if asset_id is None:
                continue
            for rate in pair_rates:
                timestamp = rate.timestamp.astimezone(UTC).isoformat()
                await wf_client.upsert_quote(
                    asset_id,
                    {
                        "timestamp": timestamp,
                        "open": str(rate.rate),
                        "high": str(rate.rate),
                        "low": str(rate.rate),
                        "close": str(rate.rate),
                        "volume": "0",
                        "currency": rate.base_currency,
                        "source": "FINANCE_SYNC",
                    },
                )
                synced += 1
        return synced

    async def _fetch_pending_transactions(
        self,
        *,
        account_id: str,
        since: datetime,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[Transaction]:
        """Fetch transactions for *account_id* that haven't been exported.

        Args:
            account_id: Finance-sync account UUID.
            since:      Lower bound on ``occurred_at`` (inclusive) used
                        when no delivery cursor exists.
            after:      Optional ``(occurred_at, transaction_id)`` resume
                        point from a delivery cursor.  When provided, only
                        transactions strictly after that point are fetched
                        — the boundary transaction is excluded and any
                        transactions sharing its exact timestamp but with a
                        later id are included (timestamp-only cursors would
                        either re-push the boundary or skip same-instant
                        transactions).
        """
        async with self._session_factory() as session:
            status_filter = ["booked"]
            if self._wf_config.include_pending:
                status_filter.append("pending")

            stmt = (
                select(Transaction)
                .where(
                    Transaction.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                    Transaction.account_id == account_id,  # type: ignore[attr-defined]
                    Transaction.status.in_(status_filter),  # type: ignore[attr-defined]
                )
                .order_by(Transaction.occurred_at)  # type: ignore[attr-defined]
            )
            if after is not None:
                cursor_ts, cursor_id = after
                stmt = stmt.where(
                    or_(
                        Transaction.occurred_at > cursor_ts,  # type: ignore[attr-defined]
                        and_(
                            Transaction.occurred_at == cursor_ts,  # type: ignore[attr-defined]
                            Transaction.id > cursor_id,  # type: ignore[attr-defined]
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    Transaction.occurred_at >= since  # type: ignore[attr-defined]
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _transaction_external_ids(self, account_id: str) -> set[str]:
        """Return all active source IDs for destination activity cleanup."""
        async with self._session_factory() as session:
            statuses = ["booked"] + (
                ["pending"] if self._wf_config.include_pending else []
            )
            result = await session.execute(
                select(Transaction.external_transaction_id).where(
                    Transaction.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                    Transaction.account_id == account_id,  # type: ignore[attr-defined]
                    Transaction.status.in_(statuses),  # type: ignore[attr-defined]
                )
            )
            return {str(value) for value in result.scalars().all()}

    async def _earliest_transaction_time(self, account_id: str) -> datetime:
        """Return the first canonical transaction date for an account.

        A new Wealthfolio destination must receive the complete source
        history.  The old 90-day fallback was only suitable for incremental
        CSV exports and caused empty performance charts after a fresh push.
        """
        async with self._session_factory() as session:
            stmt = select(func.min(Transaction.occurred_at)).where(
                Transaction.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                Transaction.account_id == account_id,  # type: ignore[attr-defined]
                Transaction.status.in_(
                    ["booked"]
                    + (["pending"] if self._wf_config.include_pending else [])
                ),  # type: ignore[attr-defined]
            )
            result = await session.execute(stmt)
            value = result.scalar_one_or_none()
        return value or _default_since()

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

    async def _fetch_historical_holdings(
        self,
        *,
        account_id: str,
    ) -> list[Holding]:
        """Fetch every source holding snapshot in observation order."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Holding)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.account_id == account_id,
                )
                .order_by(Holding.observed_at, Holding.security_id)
            )
            return list(result.scalars().all())

    async def _fetch_tax_lots(self, *, account_id: str) -> list[TaxLot]:
        """Fetch open and closed lots for one exported account."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaxLot)
                .where(
                    TaxLot.tenant_id == self._tenant_id,
                    TaxLot.account_id == account_id,
                )
                .order_by(TaxLot.acquired_at)
            )
            scalar_result = result.scalars()
            if inspect.isawaitable(scalar_result):
                scalar_result = await scalar_result
            rows = scalar_result.all()
            if inspect.isawaitable(rows):
                rows = await rows
            return list(rows)

    async def _last_export_time(self) -> datetime:
        """Return the timestamp of the last successful export.

        Defaults to 90 days ago if no previous export exists.
        """
        async with self._session_factory() as session:
            stmt = (
                select(ExportRun.started_at)
                .where(
                    ExportRun.status == "completed",  # type: ignore[attr-defined]
                    ExportRun.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                    ExportRun.target_id == self._target_id,  # type: ignore[attr-defined]
                )
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

    async def _delivery_cursor(
        self,
        *,
        account_id: str,
    ) -> tuple[datetime, UUID] | None:
        """Return the ``(occurred_at, transaction_id)`` resume point.

        Reads the per-account ``WealthfolioDelivery`` cursor.  Returns
        ``None`` when no cursor exists yet (first push for this account).
        The tuple disambiguates transactions sharing the exact timestamp:
        resuming strictly after ``(occurred_at, id)`` never re-pushes the
        boundary transaction and never skips a same-instant sibling.  The
        id is returned as a :class:`uuid.UUID` so the strict comparison
        binds correctly on every dialect (SQLite included).
        """
        delivery = await self._get_wealthfolio_delivery(account_id=account_id)
        if (
            delivery is not None
            and delivery.last_exported_at is not None
            and delivery.last_exported_transaction_id
        ):
            return (
                delivery.last_exported_at,
                UUID(delivery.last_exported_transaction_id),
            )
        return None

    async def _get_wealthfolio_delivery(
        self,
        *,
        account_id: str,
    ) -> WealthfolioDelivery | None:
        """Retrieve the WealthfolioDelivery cursor for *account_id*."""
        async with self._session_factory() as session:
            stmt = select(WealthfolioDelivery).where(
                WealthfolioDelivery.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                WealthfolioDelivery.target_id == self._target_id,  # type: ignore[attr-defined]
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
                WealthfolioDelivery.target_id == self._target_id,  # type: ignore[attr-defined]
                WealthfolioDelivery.account_id == account_id,  # type: ignore[attr-defined]
            )
            result = await session.execute(stmt)
            delivery = result.scalar_one_or_none()

            if delivery is None:
                delivery = WealthfolioDelivery(
                    tenant_id=self._tenant_id,
                    target_id=self._target_id,
                    account_id=account_id,
                    last_exported_transaction_id=str(last.id),
                    last_exported_at=last.occurred_at,
                    export_run_id=export_run_id,
                )
                session.add(delivery)
            else:
                delivery.last_exported_transaction_id = str(last.id)
                delivery.last_exported_at = last.occurred_at
                if export_run_id is not None:
                    delivery.export_run_id = export_run_id

            await session.flush()
            await session.commit()

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
                run.error_category = categorize_export_error(error_message)
            await session.flush()
            await session.commit()

    # ── Push to Wealthfolio instance ───────────────────────────────

    async def push_to_wealthfolio(
        self,
        wf_client: WealthfolioClient,
        *,
        accounts: list[Account] | None = None,
        since: datetime | None = None,
        max_transactions: int | None = None,
        full_sync: bool = False,
        rebuild: bool = False,
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
        accounts_removed = 0
        performance_account_ids: list[str] = []

        # ── Create ExportRun ──────────────────────────────────────
        async with self._session_factory() as session:
            run = ExportRun(
                tenant_id=self._tenant_id,
                status="running",
                started_at=start_ts,
                exporter_type="wealthfolio",
                target_id=self._target_id,
            )
            session.add(run)
            await session.flush()
            await session.commit()
            log = self._log.bind(export_run_id=str(run.id))

        try:
            # Delivery cursors are target-scoped. A newly-created wizard
            # destination must not inherit the legacy export run's timestamp,
            # otherwise its first push can incorrectly find zero transactions.
            _since = since or (
                await self._last_export_time()
                if self._target_id == "legacy"
                else _default_since()
            )
            fs_accounts = accounts or await self._load_accounts(None)
            security_map = await self._load_securities()

            # Wealthfolio is a projection of the selected finance-sync data.
            # Remove every account outside the exact current dataset before
            # ensuring accounts, so stale smoke/test accounts and their
            # account-owned activities/holdings cannot remain visible.
            allowed_provider_ids = {
                f"finance-sync:{self._tenant_id}:{account.id}"
                for account in fs_accounts
            }
            # Account deletion is only safe for the legacy projection. A
            # named destination can share the same Wealthfolio instance with
            # another destination; pruning there would delete accounts owned
            # by the other projection (which caused only Saxo to remain).
            if self._target_id == "legacy":
                accounts_removed = (
                    await wf_client.delete_accounts_not_owned_by_finance_sync(
                        allowed_provider_ids
                    )
                )
            if accounts_removed:
                log.info(
                    "wealthfolio_unmanaged_accounts_removed",
                    count=accounts_removed,
                )

            for fs_acct in fs_accounts:
                txns: list[Transaction] = []
                try:
                    wf_account = await self._ensure_wf_account(
                        wf_client, fs_acct
                    )
                    wf_account_id = str(wf_account["id"])
                    performance_account_ids.append(wf_account_id)
                    if full_sync or rebuild:
                        errors.extend(
                            await self._sync_historical_holdings(
                                wf_client=wf_client,
                                fs_account=fs_acct,
                                wf_account_id=wf_account_id,
                                security_map=security_map,
                            )
                        )
                    if rebuild:
                        await wf_client.delete_activities(wf_account_id)
                    else:
                        allowed_activity_ids = (
                            await self._transaction_external_ids(fs_acct.id)
                        )
                        # Keep the connector-owned cash correction across
                        # incremental syncs.  Removing it before reading
                        # holdings makes Wealthfolio briefly expose the
                        # activity-derived (and incorrect) cash balance.
                        allowed_activity_ids.add(
                            _cash_reconciliation_external_id(
                                self._tenant_id, fs_acct.id
                            )
                        )
                        await wf_client.delete_activities_not_in(
                            wf_account_id,
                            allowed_activity_ids,
                            preserved_comment_prefixes=(
                                _holdings_correction_comment_prefix(
                                    self._tenant_id, fs_acct.id
                                ),
                            ),
                        )
                    # Resume from the per-account delivery cursor when
                    # one exists (idempotent resume after partial failure).
                    # The (occurred_at, id) tuple excludes the boundary
                    # transaction so nothing already delivered is re-pushed.
                    delivery_cursor = await self._delivery_cursor(
                        account_id=fs_acct.id,
                    )
                    if full_sync or rebuild:
                        txns = await self._fetch_pending_transactions(
                            account_id=fs_acct.id,
                            since=await self._earliest_transaction_time(
                                fs_acct.id
                            ),
                        )
                    elif delivery_cursor is not None:
                        txns = await self._fetch_pending_transactions(
                            account_id=fs_acct.id,
                            since=_since,
                            after=delivery_cursor,
                        )
                    else:
                        txns = await self._fetch_pending_transactions(
                            account_id=fs_acct.id,
                            since=(
                                await self._earliest_transaction_time(
                                    fs_acct.id
                                )
                                if since is None and self._target_id != "legacy"
                                else _since
                            ),
                        )
                    if not txns:
                        errors.extend(
                            await self._sync_and_reconcile_holdings(
                                wf_client=wf_client,
                                fs_account=fs_acct,
                                wf_account_id=wf_account_id,
                                security_map=security_map,
                            )
                        )
                        continue

                    if max_transactions:
                        txns = txns[:max_transactions]

                    # Map to Wealthfolio API format
                    wf_activities: list[dict[str, Any]] = []
                    mapped_txns: list[Transaction] = []
                    for txn in txns:
                        sec = (
                            security_map.get(txn.security_id)
                            if txn.security_id
                            else None
                        )  # type: ignore[arg-type]
                        try:
                            row = map_transaction_to_wf_row(
                                txn,
                                security=sec,
                                instrument_type_map=self._wf_config.instrument_type_overrides,
                                default_currency=self._wf_config.default_currency,
                                account_currency=fs_acct.currency_code,
                                allow_multi_currency_cash=(
                                    _supports_multi_currency_cash(fs_acct)
                                ),
                                import_run_id=str(run.id),
                            )
                        except UnresolvedCashCurrencyError as exc:
                            errors.append(
                                {
                                    "account_id": fs_acct.id,
                                    "account_name": fs_acct.name,
                                    "error": str(exc),
                                }
                            )
                            # Do not advance the cursor past an activity for
                            # which the source did not provide a safe FX
                            # projection.  A later import with the missing
                            # base value can then retry it deterministically.
                            break
                        except UnresolvedSecurityExportError:
                            errors.append(
                                {
                                    "account_id": fs_acct.id,
                                    "account_name": fs_acct.name,
                                    "error": (
                                        "Security-resolutie vereist voor "
                                        f"transactie {txn.id}."
                                    ),
                                }
                            )
                            # Preserve cursor ordering: later events wait until
                            # this review item is resolved.
                            break
                        wf_activities.append(
                            _wf_row_to_api_activity(
                                row,
                                account_id=wf_account_id,
                            )
                        )
                        mapped_txns.append(txn)

                    if not wf_activities:
                        errors.extend(
                            await self._sync_and_reconcile_holdings(
                                wf_client=wf_client,
                                fs_account=fs_acct,
                                wf_account_id=wf_account_id,
                                security_map=security_map,
                            )
                        )
                        continue

                    txns_attempted += len(mapped_txns)

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
                                    f"{len(mapped_txns)} activities"
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
                        transactions=mapped_txns,
                        export_run_id=str(run.id),
                    )

                    findings = await self._sync_and_reconcile_holdings(
                        wf_client=wf_client,
                        fs_account=fs_acct,
                        wf_account_id=wf_account_id,
                        security_map=security_map,
                    )
                    errors.extend(findings)
                    errors.extend(
                        await self._reconcile_activity_totals(
                            wf_client=wf_client,
                            fs_account=fs_acct,
                            wf_account_id=wf_account_id,
                            security_map=security_map,
                        )
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

            # Historical quotes are required for Wealthfolio's performance
            # charts; transaction dates alone cannot reconstruct valuations.
            quotes_synced = await self._sync_quote_history(
                wf_client=wf_client,
                security_map=security_map,
            )
            fx_synced = await self._sync_fx_history(wf_client)
            if quotes_synced or fx_synced:
                log.info(
                    "wealthfolio_market_data_synced",
                    quotes=quotes_synced,
                    fx_rates=fx_synced,
                )

            if full_sync or rebuild:
                for account_id in performance_account_ids:
                    try:
                        history = await wf_client.get_performance_history(
                            account_id,
                            start_date="1970-01-01",
                            end_date=datetime.now(UTC).date().isoformat(),
                        )
                    except Exception as exc:
                        # Performance history is a diagnostic/read endpoint,
                        # not part of the transaction or holdings projection.
                        # Some Wealthfolio versions reject the broad date
                        # range even though the account projection is valid.
                        log.warning(
                            "wealthfolio_performance_history_unavailable",
                            account_id=account_id,
                            error=str(exc)[:256],
                        )
                        continue
                    log.info(
                        "wealthfolio_performance_history_checked",
                        account_id=account_id,
                        series_points=len(history.get("series", []))
                        if isinstance(history.get("series"), list)
                        else None,
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
                accounts_removed=accounts_removed,
            )
            return {
                "imported": txns_imported,
                "skipped": txns_skipped,
                "failed": txns_failed,
                "run_id": str(run.id),
                "errors": errors,
            }
        except asyncio.CancelledError:
            await self._complete_run(
                run,
                status="cancelled",
                error_message="Export cancelled",
                attempted=txns_attempted,
                exported=txns_imported,
                _skipped=txns_skipped,
                failed=txns_failed,
            )
            raise
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

    async def _ensure_wf_account(
        self,
        wf_client: WealthfolioClient,
        account: Account,
    ) -> dict[str, Any]:
        """Resolve one remote account using a stable provider identity."""
        provider_identity = f"finance-sync:{self._tenant_id}:{account.id}"
        name = await self._resolve_wf_account_name(account.id, account.name)
        if account.provider_key == "degiro_pension" and not name.strip():
            name = "DEGIRO Pensioen"
        remote = await wf_client.ensure_account(
            name=name,
            currency=account.currency_code or self._wf_config.default_currency,
            provider_account_id=provider_identity,
            account_type=(
                "CASH"
                if account.account_type in {"checking", "savings", "cash"}
                else "SECURITIES"
            ),
            tracking_mode="TRANSACTIONS",
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(WealthfolioAccountMapping).where(
                    WealthfolioAccountMapping.tenant_id == self._tenant_id,
                    WealthfolioAccountMapping.target_id == self._target_id,
                    WealthfolioAccountMapping.account_id == account.id,
                )
            )
            mapping = result.scalar_one_or_none()
            if mapping is None:
                mapping = WealthfolioAccountMapping(
                    tenant_id=self._tenant_id,
                    target_id=self._target_id,
                    account_id=account.id,
                    wf_account_name=str(remote.get("name") or name),
                    wf_account_id=str(remote["id"]),
                    provider_account_id=provider_identity,
                )
                session.add(mapping)
            else:
                mapping.wf_account_name = str(remote.get("name") or name)
                mapping.wf_account_id = str(remote["id"])
                mapping.provider_account_id = provider_identity
            await session.flush()
            await session.commit()
        return remote

    async def _sync_and_reconcile_holdings(
        self,
        *,
        wf_client: WealthfolioClient,
        fs_account: Account,
        wf_account_id: str,
        security_map: dict[str, Security],
    ) -> list[dict[str, str]]:
        """Bootstrap optionally, then compare current positions and value."""
        if not self._wf_config.export_holdings:
            return []
        holdings = await self._fetch_current_holdings(account_id=fs_account.id)
        source_rows: list[dict[str, Any]] = []
        findings: list[dict[str, str]] = []
        snapshot: dict[str, Any] | None = None
        for holding in holdings:
            security = security_map.get(holding.security_id)
            try:
                row = map_holding_to_wf_row(
                    holding,
                    security=security,
                    default_currency=self._wf_config.default_currency,
                )
                # Wealthfolio's market-data providers do not reliably resolve
                # exchange-qualified symbols (or ISINs) for every listing.
                # Preserve finance-sync's authoritative EUR valuation in the
                # snapshot as a manual quote so the destination shows the
                # same portfolio value even when no remote quote is available.
                if holding.market_value is not None and holding.quantity:
                    row["snapshotPrice"] = str(
                        Decimal(holding.market_value)
                        / Decimal(holding.quantity)
                    )
                source_rows.append(row)
            except UnresolvedSecurityExportError:
                findings.append(
                    {
                        "account_id": fs_account.id,
                        "account_name": fs_account.name,
                        "error": (
                            "Holding wacht op handmatige security-resolutie: "
                            f"{holding.security_id}."
                        ),
                    }
                )

        await wf_client.delete_cash_reconciliation_activities(
            wf_account_id,
            "finance-sync cash reconciliation",
        )
        # These are connector-owned, zero-price quantity corrections.  They
        # must be replaced on every run so a new broker snapshot can close a
        # position which disappeared from the current positions file.
        removed_corrections = (
            await wf_client.delete_activities_by_comment_prefix(
                wf_account_id,
                _holdings_correction_comment_prefix(
                    self._tenant_id, fs_account.id
                ),
            )
        )
        if removed_corrections:
            await asyncio.sleep(1)
        remote_rows = await wf_client.get_holdings(wf_account_id)
        # Saxo's positions export exposes the brokerage cash balance as
        # current_balance and leaves available_balance empty.  Prefer the
        # latter when present, but never omit the former from the authoritative
        # portfolio snapshot.
        available_balance = cast(
            Any,
            getattr(fs_account, "available_balance", None)
            or getattr(fs_account, "current_balance", None),
        )
        source_cash = _decimal_or_none(available_balance)
        tolerance = (
            max(
                self._wf_config.reconciliation_absolute_tolerance,
                abs(source_cash)
                * self._wf_config.reconciliation_percentage_tolerance,
            )
            if source_cash is not None
            else Decimal(0)
        )

        # The current provider snapshot is authoritative for the present
        # portfolio state.  Import it on every sync, not only for an empty
        # Wealthfolio account: activity-derived holdings can miss positions
        # that have no BUY activity in the exported history, and stale
        # positions must be removed when the provider's current set changes.
        if source_rows:
            snapshot = _holdings_snapshot_payload(
                source_rows,
                cash_balance=available_balance,
                cash_currency=fs_account.currency_code,
            )
            check = await wf_client.check_holdings_import(
                [snapshot], wf_account_id
            )
            if check.get("validationErrors"):
                findings.append(
                    {
                        "account_id": fs_account.id,
                        "account_name": fs_account.name,
                        "error": "Wealthfolio wees de holdings-preview af.",
                    }
                )
            else:
                await wf_client.save_manual_holdings(
                    _manual_holdings_payload(snapshot),
                    wf_account_id,
                    cash_balances=snapshot["cashBalances"],
                    snapshot_date=snapshot["date"],
                )
                # Wealthfolio queues the portfolio recalculation after the
                # snapshot response.  Give that job time to settle before
                # reading cash back for the final reconciliation.
                await asyncio.sleep(20)
                remote_rows = await wf_client.get_holdings(wf_account_id)

                # Activity imports and the snapshot recalculation are
                # asynchronous in Wealthfolio.  The first cash comparison can
                # therefore observe the pre-recalculation ledger.  Reconcile
                # once more after the authoritative snapshot so the final
                # projection cannot retain a stale cash balance.
                if source_cash is not None:
                    final_cash = _wealthfolio_cash_value(remote_rows)
                    final_delta = source_cash - final_cash
                    if abs(final_delta) > tolerance:
                        correction = _cash_reconciliation_activity(
                            account_id=wf_account_id,
                            account_currency=fs_account.currency_code,
                            delta=final_delta,
                            tenant_id=self._tenant_id,
                            finance_sync_account_id=fs_account.id,
                        )
                        correction_result = await wf_client.push_activities(
                            [correction]
                        )
                        if correction_result.get("failed", 0):
                            findings.append(
                                {
                                    "account_id": fs_account.id,
                                    "account_name": fs_account.name,
                                    "error": (
                                        "Wealthfolio kon de definitieve "
                                        "cashreconciliatie niet opslaan."
                                    ),
                                }
                            )
                        else:
                            remote_rows = await wf_client.get_holdings(
                                wf_account_id
                            )

        corrections = _holdings_quantity_corrections(
            source_rows=source_rows,
            remote_rows=remote_rows,
            account_id=wf_account_id,
            account_currency=fs_account.currency_code,
            tenant_id=self._tenant_id,
            finance_sync_account_id=fs_account.id,
        )
        if corrections:
            correction_result = await wf_client.push_activities(corrections)
            if correction_result.get("failed", 0):
                findings.append(
                    {
                        "account_id": fs_account.id,
                        "account_name": fs_account.name,
                        "error": (
                            "Wealthfolio kon de holdingsnapshot-correctie "
                            "niet opslaan."
                        ),
                    }
                )
            else:
                await asyncio.sleep(1)
                remote_rows = await wf_client.get_holdings(wf_account_id)
                # Wealthfolio uses the zero-price correction as the latest
                # activity quote and can consequently expose 0.01 until the
                # manual broker quote is written again.  The positions file
                # remains authoritative for current prices and valuation.
                if source_rows:
                    if snapshot is not None:
                        await wf_client.save_manual_holdings(
                            _manual_holdings_payload(snapshot),
                            wf_account_id,
                            cash_balances=snapshot["cashBalances"],
                            snapshot_date=snapshot["date"],
                        )
                    await asyncio.sleep(20)
                    remote_rows = await wf_client.get_holdings(wf_account_id)

        # Bank accounts have no securities rows, but their current balance is
        # still part of the downstream portfolio state.  A cash snapshot
        # preserves an opening balance that is not represented by the fetched
        # transaction window (common for bunq exports).
        if (
            not source_rows
            and fs_account.account_type in {"checking", "savings", "cash"}
            and fs_account.current_balance is not None
        ):
            remote_cash = sum(
                (
                    Decimal(str(row.get("marketValue", {}).get("base") or 0))
                    if isinstance(row.get("marketValue"), dict)
                    else Decimal(0)
                )
                for row in remote_rows
                if _is_cash_wealthfolio_holding(row)
            )
            expected_cash = Decimal(str(fs_account.current_balance))
            tolerance = max(
                self._wf_config.reconciliation_absolute_tolerance,
                abs(expected_cash)
                * self._wf_config.reconciliation_percentage_tolerance,
            )
            if abs(expected_cash - remote_cash) > tolerance:
                snapshot = _holdings_snapshot_payload(
                    [],
                    cash_balance=expected_cash,
                    cash_currency=fs_account.currency_code,
                )
                check = await wf_client.check_holdings_import(
                    [snapshot], wf_account_id
                )
                if check.get("validationErrors"):
                    findings.append(
                        {
                            "account_id": fs_account.id,
                            "account_name": fs_account.name,
                            "error": "Wealthfolio wees de cash-preview af.",
                        }
                    )
                else:
                    await wf_client.save_manual_holdings(
                        _manual_holdings_payload(snapshot),
                        wf_account_id,
                        cash_balances=snapshot["cashBalances"],
                        snapshot_date=snapshot["date"],
                    )
                    remote_rows = await wf_client.get_holdings(wf_account_id)

        holdings_findings = _reconcile_holdings(
            account=fs_account,
            source_rows=source_rows,
            remote_rows=remote_rows,
            absolute_tolerance=self._wf_config.reconciliation_absolute_tolerance,
            percentage_tolerance=self._wf_config.reconciliation_percentage_tolerance,
        )
        # Wealthfolio recalculates holdings asynchronously after a snapshot.
        # A short second read avoids declaring a financial failure while that
        # recalculation is still settling, without hiding a persistent delta.
        for _attempt in range(5):
            if not holdings_findings:
                break
            await asyncio.sleep(5)
            settled_rows = await wf_client.get_holdings(wf_account_id)
            holdings_findings = _reconcile_holdings(
                account=fs_account,
                source_rows=source_rows,
                remote_rows=settled_rows,
                absolute_tolerance=self._wf_config.reconciliation_absolute_tolerance,
                percentage_tolerance=self._wf_config.reconciliation_percentage_tolerance,
            )
        findings.extend(holdings_findings)
        return findings

    async def _reconcile_activity_totals(
        self,
        *,
        wf_client: WealthfolioClient,
        fs_account: Account,
        wf_account_id: str,
        security_map: dict[str, Security],
    ) -> list[dict[str, str]]:
        """Reconcile imported financial components by source identity.

        Matching by ``sourceRecordId`` makes this safe for trades whose
        Wealthfolio ``amount`` is intentionally zero (quantity x price is the
        authoritative trade value).  It also verifies the fields that most
        often create silent reporting drift: fee, tax and cash amount.
        """
        source = await self._fetch_all_active_transactions(fs_account.id)
        remote_result = wf_client.get_all_activities(wf_account_id)
        remote = (
            await remote_result
            if inspect.isawaitable(remote_result)
            else remote_result
        )
        remote_by_id: dict[str, dict[str, Any]] = {}
        for activity in remote:
            # Current Wealthfolio releases preserve idempotencyKey but may
            # normalize sourceRecordId/sourceSystem during import.  Index
            # both identities so reconciliation remains stable across API
            # versions.
            for key in ("sourceRecordId", "idempotencyKey"):
                value = activity.get(key)
                if value:
                    remote_by_id[str(value)] = activity
            comment = str(activity.get("comment") or "")
            if "ID:" in comment:
                remote_by_id[comment.split("ID:", 1)[1].strip()] = activity
        findings: list[dict[str, str]] = []
        source_cash = Decimal(0)
        remote_cash = Decimal(0)
        source_fee = Decimal(0)
        remote_fee = Decimal(0)
        source_tax = Decimal(0)
        remote_tax = Decimal(0)
        for txn in source:
            row = map_transaction_to_wf_row(
                txn,
                security=(
                    security_map.get(txn.security_id)
                    if txn.security_id
                    else None
                ),
                account_currency=fs_account.currency_code,
                default_currency=self._wf_config.default_currency,
            )
            activity = remote_by_id.get(str(row["idempotencyKey"]))
            if activity is None:
                activity = remote_by_id.get(str(txn.external_transaction_id))
            if activity is None:
                findings.append(
                    {
                        "account_id": fs_account.id,
                        "account_name": fs_account.name,
                        "error": (
                            "Brontransactie ontbreekt in Wealthfolio: "
                            f"{txn.external_transaction_id}."
                        ),
                    }
                )
                continue
            if txn.transaction_type not in {"purchase", "sale"}:
                source_cash += Decimal(str(row["amount"] or 0))
                remote_cash += Decimal(str(activity.get("amount") or 0))
            source_fee += Decimal(str(row["fee"] or 0))
            remote_fee += Decimal(str(activity.get("fee") or 0))
            source_tax += Decimal(str(row["tax"] or 0))
            remote_tax += Decimal(str(activity.get("tax") or 0))

        def differs(left: Decimal, right: Decimal) -> bool:
            tolerance = max(
                self._wf_config.reconciliation_absolute_tolerance,
                abs(left) * self._wf_config.reconciliation_percentage_tolerance,
            )
            return abs(left - right) > tolerance

        for label, left, right in (
            ("bruto cashbedrag", source_cash, remote_cash),
            ("fees", source_fee, remote_fee),
            ("belastingen", source_tax, remote_tax),
        ):
            if differs(left, right):
                findings.append(
                    {
                        "account_id": fs_account.id,
                        "account_name": fs_account.name,
                        "error": (
                            f"Reconciliatie {label} wijkt af "
                            f"({left} versus {right})."
                        ),
                    }
                )
        return findings

    async def _fetch_all_active_transactions(
        self, account_id: str
    ) -> list[Transaction]:
        """Load all source transactions included in the current projection."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Transaction)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.account_id == account_id,
                    Transaction.status.in_(
                        ["booked"]
                        + (
                            ["pending"]
                            if self._wf_config.include_pending
                            else []
                        )
                    ),
                )
                .order_by(Transaction.occurred_at)
            )
            scalar_result = result.scalars()
            if inspect.isawaitable(scalar_result):
                scalar_result = await scalar_result
            rows = scalar_result.all()
            if inspect.isawaitable(rows):
                rows = await rows
            return list(rows)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _supports_multi_currency_cash(account: Account) -> bool:
    """Return the connector-declared cash capability for an account."""
    metadata = account.provider_metadata
    if (
        isinstance(metadata, dict)
        and "supports_multi_currency_cash" in metadata
    ):
        return bool(metadata["supports_multi_currency_cash"])
    return False


def _decimal_or_none(value: Any) -> Decimal | None:
    """Convert a provider balance without allowing malformed values to leak."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _wealthfolio_cash_value(rows: list[dict[str, Any]]) -> Decimal:
    """Sum Wealthfolio cash holdings in the account/base currency."""
    total = Decimal(0)
    for row in rows:
        if not _is_cash_wealthfolio_holding(row):
            continue
        market_value = row.get("marketValue")
        if isinstance(market_value, dict):
            typed_market_value = cast("dict[str, Any]", market_value)
            total += Decimal(str(typed_market_value.get("base") or 0))
    return total


def _cash_reconciliation_activity(
    *,
    account_id: str,
    account_currency: str,
    delta: Decimal,
    tenant_id: str,
    finance_sync_account_id: str,
) -> dict[str, Any]:
    """Build an owned deposit/withdrawal that closes the current cash delta."""
    activity_type = "DEPOSIT" if delta > 0 else "WITHDRAWAL"
    correction_id = _cash_reconciliation_external_id(
        tenant_id, finance_sync_account_id
    )
    return {
        "activityType": activity_type,
        "date": datetime.now(UTC).date().isoformat(),
        "symbol": "",
        "accountId": account_id,
        "quantity": 1.0,
        "amount": float(abs(delta)),
        "currency": account_currency,
        "quoteCcy": account_currency,
        "comment": (f"finance-sync cash reconciliation | ID: {correction_id}"),
        "isDraft": False,
        "isValid": True,
    }


def _cash_reconciliation_external_id(
    tenant_id: UUID | str, finance_sync_account_id: UUID | str
) -> str:
    """Return the stable source ID of the connector-owned cash correction."""
    return (
        f"finance-sync-cash-reconciliation:{tenant_id}:"
        f"{finance_sync_account_id}"
    )


def _holdings_correction_comment_prefix(
    tenant_id: UUID | str, finance_sync_account_id: UUID | str
) -> str:
    return (
        "finance-sync holdings snapshot reconciliation:"
        f"{tenant_id}:{finance_sync_account_id}:"
    )


def _holdings_quantity_corrections(
    *,
    source_rows: list[dict[str, Any]],
    remote_rows: list[dict[str, Any]],
    account_id: str,
    account_currency: str,
    tenant_id: UUID | str,
    finance_sync_account_id: UUID | str,
) -> list[dict[str, Any]]:
    """Create idempotent zero-price activities for the authoritative snapshot.

    Wealthfolio derives holdings from activities.  A broker positions file is
    nevertheless the authority for *current quantity*: it can contain an
    opening position without a BUY in the imported history and it can omit a
    closed position.  BUY/SELL with a zero price changes quantity without
    changing cash (unlike ADJUSTMENT, which the live API ignores for holdings).
    """
    precision = Decimal("0.01")
    prefix = _holdings_correction_comment_prefix(
        tenant_id, finance_sync_account_id
    )

    def aliases(row: dict[str, Any]) -> set[str]:
        instrument = row.get("instrument")
        instrument = (
            cast(dict[str, Any], instrument)
            if isinstance(instrument, dict)
            else {}
        )
        values = (
            row.get("symbol"),
            row.get("isin"),
            row.get("displayCode"),
            instrument.get("symbol"),
            instrument.get("isin"),
            instrument.get("displayCode"),
        )
        return {
            _normalise_wealthfolio_symbol(str(value))
            for value in values
            if value
        }

    source: list[tuple[dict[str, Any], set[str], Decimal]] = []
    for row in source_rows:
        identity = aliases(row)
        if identity:
            source.append(
                (
                    row,
                    identity,
                    Decimal(str(row["quantity"])).quantize(precision),
                )
            )

    remote: list[tuple[dict[str, Any], set[str], Decimal]] = []
    for row in remote_rows:
        if _is_cash_wealthfolio_holding(row):
            continue
        identity = aliases(row)
        if identity:
            remote.append(
                (
                    row,
                    identity,
                    Decimal(str(row.get("quantity") or 0)).quantize(precision),
                )
            )

    used_remote: set[int] = set()
    targets: list[tuple[str, dict[str, Any], Decimal, Decimal]] = []
    for source_row, source_aliases, target in source:
        match_index = next(
            (
                index
                for index, (_, remote_aliases, _) in enumerate(remote)
                if index not in used_remote and source_aliases & remote_aliases
            ),
            None,
        )
        current = Decimal(0)
        correction_row = source_row
        if match_index is not None:
            used_remote.add(match_index)
            current = remote[match_index][2]
            correction_row = {**source_row, **remote[match_index][0]}
        stable_id = next(iter(sorted(source_aliases)))
        targets.append((stable_id, correction_row, target, current))

    # Any remote position not represented by today's positions file is
    # closed/stale and must be driven to zero.  No quote is created for it.
    for index, (remote_row, remote_aliases, current) in enumerate(remote):
        if index in used_remote:
            continue
        stable_id = next(iter(sorted(remote_aliases)))
        targets.append((stable_id, remote_row, Decimal(0), current))

    corrections: list[dict[str, Any]] = []
    for stable_id, row, target, current in targets:
        delta = target - current
        if delta == 0:
            continue
        instrument = row.get("instrument")
        instrument = (
            cast(dict[str, Any], instrument)
            if isinstance(instrument, dict)
            else {}
        )
        symbol = str(row.get("symbol") or instrument.get("symbol") or "")
        isin = str(row.get("isin") or instrument.get("isin") or "")
        currency = str(row.get("currency") or account_currency)
        correction_id = f"{prefix}{stable_id}"
        corrections.append(
            {
                "activityType": "BUY" if delta > 0 else "SELL",
                "date": datetime.now(UTC).date().isoformat(),
                "accountId": account_id,
                "assetId": instrument.get("id") or row.get("assetId"),
                "symbol": symbol,
                "isin": isin,
                "instrumentType": str(
                    instrument.get("instrumentType") or "EQUITY"
                ),
                "quantity": float(abs(delta)),
                "unitPrice": 0.0,
                "currency": currency,
                "quoteCcy": currency,
                "comment": f"{correction_id} TARGET:{target}",
                "sourceSystem": "FINANCE_SYNC",
                "sourceRecordId": correction_id,
                "sourceGroupId": (
                    f"finance-sync:holdings:{finance_sync_account_id}"
                ),
                "idempotencyKey": correction_id,
                "isDraft": False,
                "isValid": True,
            }
        )
    return corrections


def _default_since() -> datetime:
    """Return 90 days before now (UTC)."""
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=90)


def _wf_row_to_api_activity(
    row: dict[str, Any], *, account_id: str | None = None
) -> dict[str, Any]:
    """Convert a Wealthfolio CSV row dict to API activity format.

    The transaction mapper produces CSV-row dicts with string-formatted
    values.  This helper converts them to the types the Wealthfolio
    REST API expects (numbers as floats, booleans as needed).
    """
    activity: dict[str, Any] = {
        "activityType": row.get("activityType", ""),
        "date": row.get("date", ""),
        "symbol": row.get("symbol", ""),
        "accountId": account_id,
        "isDraft": False,
        "isValid": True,
    }

    # Symbol — blank for cash activities
    symbol = row.get("symbol", "")
    if symbol and _looks_like_isin(str(symbol)):
        activity["isin"] = symbol
    elif row.get("isin"):
        activity["isin"] = row["isin"]

    # Numeric fields
    for numeric_key in ("quantity", "unitPrice", "amount", "fee", "fxRate"):
        val = row.get(numeric_key, "")
        if (
            val != ""
            and val is not None
            and not (
                numeric_key == "amount"
                and row.get("activityType") in {"BUY", "SELL"}
                and float(val) == 0
            )
        ):
            with contextlib.suppress(ValueError, TypeError):
                activity[numeric_key] = float(val)

    # String fields
    for str_key in (
        "currency",
        "comment",
        "instrumentType",
        "symbolName",
        "exchangeMic",
        "providerId",
        "providerSymbol",
        "settlementDate",
        "status",
        "subtype",
        "sourceType",
        "grossAmount",
        "netAmount",
        "sourceSystem",
        "sourceRecordId",
        "sourceGroupId",
        "idempotencyKey",
        "importRunId",
    ):
        val = row.get(str_key, "")
        if val:
            activity[str_key] = val
    if row.get("metadata"):
        activity["metadata"] = row["metadata"]
    if row.get("tax") not in (None, ""):
        activity["tax"] = float(row["tax"])
    if "needsReview" in row:
        value = row["needsReview"]
        activity["needsReview"] = (
            value
            if isinstance(value, bool)
            else str(value).strip().lower() in {"1", "true", "yes"}
        )

    # Price currency — required by the import endpoint.  The check
    # endpoint auto-resolves it from the symbol, but the import call
    # rejects activities without an explicit ``quoteCcy`` (recorded
    # 2026-08-16 against the live instance).  For a transaction
    # denominated in its own currency the price currency equals the
    # transaction currency; for FX-converted trades the mapper carries
    # the quote currency in ``currency`` and the base in ``fxRate``.
    quote_ccy = row.get("currency") or activity.get("currency")
    if quote_ccy:
        activity["quoteCcy"] = quote_ccy

    return activity


def _looks_like_isin(value: str) -> bool:
    return len(value) == 12 and value[:2].isalpha() and value[2:].isalnum()


def _holdings_snapshot_payload(
    rows: list[dict[str, Any]],
    *,
    cash_balance: Decimal | None,
    cash_currency: str,
) -> dict[str, Any]:
    """Build Wealthfolio's holdings snapshot contract without double value."""
    # This is a current-position snapshot, not a historical transaction
    # snapshot.  Using the last broker observation date can leave the
    # positions invisible in Wealthfolio's current holdings view when the
    # export runs later.
    snapshot_date = datetime.now(UTC).date().isoformat()
    positions = [
        {
            "symbol": row["symbol"],
            "isin": row.get("isin", ""),
            "quantity": row["quantity"],
            "avgCost": row["avgCost"] or None,
            "currency": row["currency"],
            "quoteMode": "MANUAL" if row.get("snapshotPrice") else "MARKET",
            **(
                {"price": row["snapshotPrice"]}
                if row.get("snapshotPrice")
                else {}
            ),
        }
        for row in rows
    ]
    cash = (
        {cash_currency: str(cash_balance)}
        if cash_balance is not None and cash_balance != 0
        else {}
    )
    return {"date": snapshot_date, "positions": positions, "cashBalances": cash}


def _manual_holdings_payload(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an internal snapshot to Wealthfolio's live holdings shape."""
    return [
        {
            "symbol": position["symbol"],
            "_securityIsin": position.get("isin", ""),
            "quantity": str(position["quantity"]),
            # The live snapshots endpoint expects the cost basis per unit
            # under ``averageCost``.  ``unitPrice`` is the current valuation
            # price used by the manual quote projection and is not a valid
            # replacement for the lot cost basis.
            "averageCost": position["avgCost"] or None,
            # Wealthfolio stores manual quantities at two decimal places.
            # Recalculate the manual unit price against that quantity so the
            # displayed market value remains the finance-sync source value.
            "unitPrice": str(
                (
                    Decimal(str(position["price"]))
                    * Decimal(str(position["quantity"]))
                    / Decimal(str(position["quantity"])).quantize(
                        Decimal("0.01")
                    )
                )
                if position.get("price")
                else position["avgCost"]
            ),
            "sourceValue": str(
                Decimal(str(position["price"]))
                * Decimal(str(position["quantity"]))
            )
            if position.get("price")
            else None,
            "currency": position["currency"],
            "quoteMode": "MANUAL",
        }
        for position in snapshot["positions"]
    ]


def _is_cash_wealthfolio_holding(row: dict[str, Any]) -> bool:
    """Return whether a Wealthfolio holding is the synthetic cash row."""
    raw_instrument = row.get("instrument")
    instrument = (
        cast(dict[str, Any], raw_instrument)
        if isinstance(raw_instrument, dict)
        else {}
    )
    return str(row.get("holdingType") or "").lower() == "cash" or str(
        instrument.get("id") or ""
    ).startswith("cash:")


def _reconcile_holdings(
    *,
    account: Account,
    source_rows: list[dict[str, Any]],
    remote_rows: list[dict[str, Any]],
    absolute_tolerance: Decimal,
    percentage_tolerance: Decimal,
) -> list[dict[str, str]]:
    """Return visible, path-free reconciliation findings."""
    findings: list[dict[str, str]] = []
    source_positions: list[tuple[set[str], Decimal, str]] = []
    source_identities: set[str] = set()
    for row in source_rows:
        quantity = Decimal(str(row["quantity"]))
        # The import payload is ticker-first, while Wealthfolio commonly
        # resolves the same instrument to its ISIN in holdings/list. Compare
        # both stable identities so this is a representation difference, not
        # a false position mismatch.
        identities = {
            _normalise_wealthfolio_symbol(str(identity))
            for identity in (row.get("symbol"), row.get("isin"))
            if identity
        }
        if identities:
            source_positions.append(
                (identities, quantity, str(row.get("symbol") or ""))
            )
            source_identities.update(identities)
    remote_quantities: dict[str, Decimal] = {}
    remote_value = Decimal(0)
    for row in remote_rows:
        raw_instrument = row.get("instrument")
        instrument: dict[str, Any] = (
            cast("dict[str, Any]", raw_instrument)
            if isinstance(raw_instrument, dict)
            else {}
        )
        is_cash = str(row.get("holdingType") or "").lower() == "cash" or str(
            instrument.get("id") or ""
        ).startswith("cash:")
        # Cash is tracked as the account balance in finance-sync, not as a
        # holdings row — it must not count as a position outside the source
        # snapshot (the live Wealthfolio instance returns a holdingType
        # "cash" row with the currency code as its symbol).
        if not is_cash:
            quantity = Decimal(str(row.get("quantity") or 0))
            # Wealthfolio can expose both the resolved ISIN and the original
            # ticker. Retain both aliases so reconciliation is independent of
            # which representation the remote resolver chose.
            for identity in (
                instrument.get("isin"),
                instrument.get("symbol"),
                instrument.get("displayCode"),
            ):
                if identity:
                    remote_quantities[
                        _normalise_wealthfolio_symbol(str(identity))
                    ] = quantity
        raw_market_value = row.get("marketValue")
        market_value: dict[str, Any] = (
            cast("dict[str, Any]", raw_market_value)
            if isinstance(raw_market_value, dict)
            else {}
        )
        remote_value += Decimal(str(market_value.get("base") or 0))

    source_value: Decimal | None = None
    if source_rows and all("snapshotPrice" in row for row in source_rows):
        source_value = sum(
            (
                Decimal(str(row["quantity"]))
                * Decimal(str(row["snapshotPrice"]))
                for row in source_rows
            ),
            Decimal(0),
        )
        cash = _decimal_or_none(
            getattr(account, "available_balance", None)
            or getattr(account, "current_balance", None)
        )
        if cash is not None:
            source_value += cash
    elif (
        not source_rows
        and getattr(account, "current_balance", None) is not None
    ):
        source_value = _decimal_or_none(
            getattr(account, "current_balance", None)
        )

    for identities, quantity, display_symbol in source_positions:
        remote_quantity = next(
            (
                remote_quantities[identity]
                for identity in identities
                if identity in remote_quantities
            ),
            None,
        )
        quantity_tolerance = max(
            absolute_tolerance,
            abs(quantity) * percentage_tolerance,
            Decimal("0.01"),
        )
        if (
            remote_quantity is None
            or abs(remote_quantity - quantity) > quantity_tolerance
        ):
            findings.append(
                {
                    "account_id": account.id,
                    "account_name": account.name,
                    "error": (
                        "Positie-afwijking voor opgeloste security "
                        f"{display_symbol}."
                    ),
                }
            )
    extra = set(remote_quantities) - source_identities
    if extra:
        findings.append(
            {
                "account_id": account.id,
                "account_name": account.name,
                "error": "Wealthfolio bevat posities buiten de bronsnapshot.",
            }
        )

    # Compare the complete portfolio value when the source supplied enough
    # valuation data.  For brokerage accounts this includes securities plus
    # cash; comparing only ``current_balance`` would be incorrect.
    if source_value is not None and remote_rows:
        difference = abs(source_value - remote_value)
        tolerance = max(
            absolute_tolerance,
            abs(source_value) * percentage_tolerance,
        )
        if difference > tolerance:
            findings.append(
                {
                    "account_id": account.id,
                    "account_name": account.name,
                    "error": "Portefeuillewaarde buiten ingestelde tolerantie.",
                }
            )
    return findings


def _normalise_wealthfolio_symbol(symbol: str) -> str:
    """Compare exchange-qualified tickers by their Wealthfolio symbol.

    Trading212 fixtures can identify a security as ``VWCE.DE`` or
    ``ASML.NL`` while Wealthfolio resolves those instruments to ``VWCE`` and
    ``ASML``.  ISINs are left untouched; only non-ISIN tickers are reduced to
    the symbol Wealthfolio exposes in ``holdings/list``.
    """
    value = symbol.strip().upper()
    if _looks_like_isin(value):
        return value
    return value.split(".", 1)[0]
