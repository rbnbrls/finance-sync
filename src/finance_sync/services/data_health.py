"""Canonical aggregation for the user-facing Data health workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, exists, func, or_, select

from finance_sync.exporter.models import ExportRun
from finance_sync.models import (
    Account,
    EnrichmentFreshness,
    Holding,
    ImportRun,
    Security,
    TaxLot,
    Transaction,
)
from finance_sync.schemas.data_health import (
    DataHealthIssue,
    DataHealthOverview,
    DataHealthReconciliation,
    DataHealthSource,
    DataHealthStatus,
)
from finance_sync.services.control_plane import ControlPlaneService
from finance_sync.services.control_plane_actions import action
from finance_sync.services.data_quality import DataQualityService

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.schemas.control_plane import ControlPlaneIssue


class DataHealthService:
    """Compose existing operational projections into one actionable contract."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: str,
        *,
        permissions: set[str] | None = None,
        redis_configured: bool = False,
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._permissions = permissions
        self._redis_configured = redis_configured
        self._now = now or datetime.now(UTC)

    async def get_overview(self) -> DataHealthOverview:
        control = await ControlPlaneService(
            self._session,
            self._tenant_id,
            permissions=self._permissions,
            redis_configured=self._redis_configured,
            now=self._now,
        ).get_overview()
        quality = await DataQualityService(
            self._session, self._tenant_id, now=self._now
        ).get_overview()

        issues = [self._issue(issue) for issue in control.issues]
        sources = [
            DataHealthSource(
                id=connection.id,
                provider=connection.provider,
                status=connection.status,
                last_success_at=connection.last_success_at,
                last_attempt_at=connection.last_attempt_at,
                accounts=next(
                    (
                        coverage.accounts
                        for coverage in quality.coverage
                        if coverage.provider == connection.provider
                    ),
                    0,
                ),
                transactions=next(
                    (
                        coverage.transactions
                        for coverage in quality.coverage
                        if coverage.provider == connection.provider
                    ),
                    0,
                ),
            )
            for connection in control.connections
        ]
        issues.extend(self._missing_source_issues(sources, control))
        issues.extend(await self._changed_provider_issues(sources))
        issues.extend(await self._additional_issues())
        # A few lightweight callers intentionally construct this composer
        # without a database session; preserve the projection-only mode.
        if getattr(self, "_session", None) is not None:
            issues.extend(await self._canonical_data_issues())
            issues.extend(await self._wealthfolio_preflight_issues())
        stale_data = {
            "securities_stale": control.freshness.securities_stale,
            "securities_without_quote": (
                control.freshness.securities_without_quote
            ),
            "holdings_without_valuation": (
                control.freshness.holdings_without_valuation
            ),
        }
        return DataHealthOverview(
            status=self._status(control.status, quality.status, issues),
            last_successful_sync=max(
                (
                    source.last_success_at
                    for source in sources
                    if source.last_success_at
                ),
                default=None,
            ),
            sources=sources,
            stale_data=stale_data,
            unresolved_securities=sum(
                1 for issue in issues if issue.category == "unresolved_security"
            ),
            failed_exports=sum(
                1
                for issue in issues
                if issue.category in {"export", "destination", "failed_export"}
            ),
            reconciliation=DataHealthReconciliation(
                findings_total=quality.findings_total,
                findings_by_kind=quality.findings_by_kind,
                latest_run_at=quality.latest_run_at,
            ),
            issues=issues,
            as_of=control.as_of,
            generated_at=self._now,
        )

    def _missing_source_issues(
        self,
        sources: list[DataHealthSource],
        control: object,
    ) -> list[DataHealthIssue]:
        """Create a sync action when a healthy configured source has no data."""
        connections = getattr(control, "connections", [])
        issues: list[DataHealthIssue] = []
        if not sources:
            return [
                DataHealthIssue(
                    id="missing-sources:configured",
                    category="missing_transactions",
                    severity="warning",
                    title="Nog geen financiële bron geconfigureerd",
                    description=(
                        "Voeg ten minste één bron toe om transacties en saldi "
                        "te kunnen synchroniseren."
                    ),
                    source="connections",
                    action=action(
                        "view_connection",
                        "/api/v1/connectors/configs",
                        permissions=self._permissions,
                    ),
                )
            ]
        for source, connection in zip(sources, connections, strict=True):
            if source.transactions:
                continue
            is_file_source = connection.provider in {
                "degiro_pension",
                "saxo_investor",
            }
            if source.status != "healthy" and not is_file_source:
                continue
            if is_file_source:
                source_action = action(
                    "open_file_import",
                    f"#uploads?provider={connection.provider}",
                    permissions=self._permissions,
                )
                description = (
                    f"De bestandsbron {source.provider} heeft nog geen "
                    "transacties. Upload de actuele provider-export om de "
                    "dataset aan te vullen."
                )
            else:
                source_action = next(
                    (
                        item
                        for item in connection.actions
                        if item.key == "sync_connection"
                    ),
                    action(
                        "sync_connection",
                        f"/api/v1/sync/connections/{source.id}",
                        permissions=self._permissions,
                    ),
                )
                description = (
                    f"De gezonde bron {source.provider} heeft nog geen "
                    "transacties in de canonieke dataset."
                )
            issues.append(
                DataHealthIssue(
                    id=f"missing-transactions:{source.id}",
                    category="missing_transactions",
                    severity="warning",
                    title="Bron bevat nog geen transacties",
                    description=description,
                    provider=source.provider,
                    source="transactions",
                    action=source_action,
                )
            )
        return issues

    async def _wealthfolio_preflight_issues(self) -> list[DataHealthIssue]:
        """Find defects that Wealthfolio cannot represent safely.

        Wealthfolio derives performance from activities, valuations and
        market-data records.  These checks run against the canonical source
        data before export, so the GUI can explain and repair the input rather
        than exposing a misleading destination-side warning.
        """
        issues: list[DataHealthIssue] = []

        quote_rows = (
            await self._session.execute(
                select(Security.name, EnrichmentFreshness.error_message)
                .join(
                    EnrichmentFreshness,
                    EnrichmentFreshness.security_id == Security.id,
                )
                .join(Holding, Holding.security_id == Security.id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    EnrichmentFreshness.status == "failed",
                    EnrichmentFreshness.data_source == "wealthfolio",
                )
                .distinct()
                .order_by(Security.name)
            )
        ).all()
        if quote_rows:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:quote-sync-failures",
                    category="quote_sync_failure",
                    severity="error",
                    title="Koerssync naar Wealthfolio mislukt",
                    description=(
                        "Een of meer actuele of historische koersen konden "
                        "niet worden opgeslagen. Controleer ISIN, beurs, "
                        "valuta en de ingestelde marktdata-provider voordat "
                        "je exporteert."
                    ),
                    impact_count=len(quote_rows),
                    source="market_data",
                    details=[
                        f"{name}: {error or 'onbekende fout'}"
                        for name, error in quote_rows[:10]
                    ],
                    action=action(
                        "refresh_quotes",
                        "/api/v1/enrichment/refresh-quotes",
                        permissions=self._permissions,
                    ),
                )
            )

        negative_rows = (
            await self._session.execute(
                select(
                    Account.name,
                    func.date(Holding.observed_at),
                    func.sum(Holding.market_value),
                )
                .join(Account, Account.id == Holding.account_id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.market_value.is_not(None),
                )
                .group_by(Account.name, func.date(Holding.observed_at))
                .having(func.sum(Holding.market_value) < 0)
                .order_by(func.date(Holding.observed_at))
            )
        ).all()
        cash_rows = (
            await self._session.execute(
                select(
                    Transaction.account_id,
                    Account.name,
                    Transaction.amount,
                    Transaction.occurred_at,
                    Transaction.transaction_type,
                )
                .join(Account, Account.id == Transaction.account_id)
                .where(Transaction.tenant_id == self._tenant_id)
                .order_by(Transaction.account_id, Transaction.occurred_at)
            )
        ).all()
        running_cash: dict[str, Decimal] = {}
        negative_cash: list[tuple[str, str, object, Decimal]] = []
        for (
            account_id,
            name,
            amount,
            occurred_at,
            _transaction_type,
        ) in cash_rows:
            balance = running_cash.get(str(account_id), Decimal(0))
            balance += Decimal(str(amount))
            running_cash[str(account_id)] = balance
            if balance < 0:
                negative_cash.append(
                    (str(account_id), str(name), occurred_at, balance)
                )
        negative_history = [*negative_rows, *negative_cash]
        if negative_history:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:negative-valuation-history",
                    category="negative_valuation",
                    severity="error",
                    title="Negatieve portfoliowaardering in historie",
                    description=(
                        "Wealthfolio kan rendement niet betrouwbaar berekenen "
                        "als een historische waardering onder nul komt. Dit "
                        "wijst meestal op ontbrekende aankoop-, stortings- of "
                        "transferactiviteiten."
                    ),
                    impact_count=len(negative_history),
                    source="holdings_and_transactions",
                    details=[
                        (
                            f"{row[0]} · {row[1]}: {row[2]}"
                            if len(row) == 3
                            else f"{row[1]} · {row[2]}: {row[3]}"
                        )
                        for row in negative_history[:10]
                    ],
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions",
                        permissions=self._permissions,
                    ),
                )
            )

        valuation_rows = (
            await self._session.execute(
                select(Holding.id, Account.name, Security.name)
                .join(Account, Account.id == Holding.account_id)
                .join(Security, Security.id == Holding.security_id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.quantity != 0,
                    Holding.market_value.is_(None),
                    Holding.price.is_(None),
                )
                .order_by(Holding.observed_at.desc())
                .limit(1000)
            )
        ).all()
        if valuation_rows:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:incomplete-valuations",
                    category="incomplete_valuation",
                    severity="error",
                    title="Waarderingsregels zijn niet compleet",
                    description=(
                        "Elke snapshot die naar Wealthfolio gaat moet een "
                        "marktwaarde of een betrouwbare koers hebben. Vul de "
                        "koers aan of accepteer de security expliciet als "
                        "marktdata niet beschikbaar is."
                    ),
                    impact_count=len(valuation_rows),
                    source="holdings",
                    details=[
                        f"{name} in {account}"
                        for _id, account, name in valuation_rows[:10]
                    ],
                    action=action(
                        "refresh_quotes",
                        "/api/v1/enrichment/refresh-quotes",
                        permissions=self._permissions,
                    ),
                )
            )

        latest_holdings = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_observed_at"),
            )
            .where(
                Holding.tenant_id == self._tenant_id,
                Holding.quantity != 0,
            )
            .group_by(Holding.account_id, Holding.security_id)
            .subquery()
        )
        cost_rows = (
            await self._session.execute(
                select(Holding.id, Account.name, Security.name)
                .join(Account, Account.id == Holding.account_id)
                .join(Security, Security.id == Holding.security_id)
                .join(
                    latest_holdings,
                    and_(
                        latest_holdings.c.account_id == Holding.account_id,
                        latest_holdings.c.security_id == Holding.security_id,
                        latest_holdings.c.latest_observed_at
                        == Holding.observed_at,
                    ),
                )
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.cost_basis.is_(None),
                    ~exists(
                        select(TaxLot.id).where(
                            TaxLot.tenant_id == self._tenant_id,
                            TaxLot.account_id == Holding.account_id,
                            TaxLot.security_id == Holding.security_id,
                            TaxLot.remaining_quantity > 0,
                        )
                    ),
                )
                .order_by(Security.name)
                .limit(1000)
            )
        ).all()
        if cost_rows:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:incomplete-cost-basis",
                    category="incomplete_cost_basis",
                    severity="warning",
                    title="Posities missen cost basis",
                    description=(
                        "Wealthfolio kan de marktwaarde tonen, maar geen "
                        "betrouwbare winst/verliesberekening maken zonder "
                        "verkrijgingsprijs. Herstel de aankoopactiviteiten of "
                        "leg de verkrijgingsprijs vast."
                    ),
                    impact_count=len(cost_rows),
                    source="holdings",
                    details=[
                        f"{name} in {account}"
                        for _id, account, name in cost_rows[:10]
                    ],
                    affected_transaction_ids=[],
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?type=purchase",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _additional_issues(self) -> list[DataHealthIssue]:
        """Detect account conflicts, incomplete imports and exports."""
        issues: list[DataHealthIssue] = []

        # ExportRun is the authoritative run ledger for both legacy
        # environment-based exports and wizard-created destinations.  The
        # control-plane destination projection only knows wizard targets, so
        # querying the ledger here also exposes legacy Wealthfolio failures on
        # the Data health page.
        export_rows = (
            (
                await self._session.execute(
                    select(ExportRun)
                    .where(ExportRun.tenant_id == self._tenant_id)
                    .order_by(ExportRun.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        seen_exports: set[tuple[str, str]] = set()
        for export_run in export_rows:
            # Destination-backed runs are already projected by the control
            # plane with the destination retry action. Keep this legacy
            # ledger projection only for environment-based exports so one
            # failed run cannot appear twice in Data health.
            if export_run.target_id and export_run.target_id != "legacy":
                continue
            # A worker restart can leave a terminal cancellation newer than
            # the actual failed attempt. Do not let that housekeeping record
            # hide the latest actionable failed/completed export outcome.
            if export_run.status == "cancelled":
                continue
            export_key = (
                str(export_run.exporter_type),
                str(export_run.target_id or "legacy"),
            )
            if export_key in seen_exports:
                continue
            seen_exports.add(export_key)
            if export_run.status not in {"failed", "running"}:
                continue
            is_running = export_run.status == "running"
            exporter_name = str(export_run.exporter_type).replace("-", " ")
            issues.append(
                DataHealthIssue(
                    id=f"export-run:{export_run.id}",
                    category=("export" if is_running else "failed_export"),
                    severity="warning" if is_running else "error",
                    title=(
                        f"{exporter_name.title()}-export is bezig"
                        if is_running
                        else f"{exporter_name.title()}-export mislukt"
                    ),
                    description=(
                        "Een export-run is nog actief. Controleer de run als "
                        "deze ongewoon lang blijft lopen."
                        if is_running
                        else (
                            "De laatste export-run is mislukt. Bekijk de "
                            "runhistorie en start daarna een gecontroleerde "
                            "retry."
                        )
                    ),
                    provider=str(export_run.exporter_type),
                    source="export_runs",
                    action=action(
                        "view_export" if is_running else "retry_export",
                        (
                            f"/api/v1/exporters/runs/{export_run.id}"
                            if is_running
                            else (
                                f"/api/v1/exporters/"
                                f"{export_run.exporter_type}/runs/"
                                f"{export_run.id}/retry"
                            )
                        ),
                        permissions=self._permissions,
                    ),
                    details=(
                        [
                            f"status={export_run.status}",
                            f"started_at={export_run.started_at.isoformat()}",
                        ]
                        if export_run.started_at
                        else []
                    ),
                )
            )

        account_rows = (
            await self._session.execute(
                select(
                    Account.provider_key,
                    Account.external_account_id,
                    func.count(Account.id),
                    func.min(Account.current_balance),
                    func.max(Account.current_balance),
                )
                .where(
                    Account.tenant_id == self._tenant_id,
                    Account.is_active.is_(True),
                )
                .group_by(Account.provider_key, Account.external_account_id)
                .having(func.count(Account.id) > 1)
            )
        ).all()
        for provider, external_id, count, minimum, maximum in account_rows:
            identity = f"{provider}:{external_id}"
            issues.append(
                DataHealthIssue(
                    id=f"duplicate-account:{identity}",
                    category="duplicate_accounts",
                    severity="warning",
                    title="Mogelijke dubbele account",
                    description=(
                        f"{count} actieve accounts delen dezelfde "
                        "provideridentiteit."
                    ),
                    impact_count=int(count),
                    provider=str(provider),
                    source="accounts",
                    action=action(
                        "view_accounts",
                        "/api/v1/accounts",
                        permissions=self._permissions,
                    ),
                )
            )
            if (
                minimum is not None
                and maximum is not None
                and minimum != maximum
            ):
                issues.append(
                    DataHealthIssue(
                        id=f"balance-conflict:{identity}",
                        category="balance_conflict",
                        severity="error",
                        title="Conflicterende saldi",
                        description=(
                            "Dezelfde provideraccount heeft verschillende "
                            "actuele "
                            "saldo's in de canonieke dataset."
                        ),
                        impact_count=int(count),
                        provider=str(provider),
                        source="accounts",
                        action=action(
                            "view_accounts",
                            "/api/v1/accounts",
                            permissions=self._permissions,
                        ),
                    )
                )

        import_runs = (
            await self._session.execute(
                select(ImportRun)
                .where(
                    ImportRun.tenant_id == self._tenant_id,
                    ImportRun.status.in_(
                        ["failed", "partial", "incomplete", "quarantined"]
                    ),
                )
                .order_by(ImportRun.created_at.desc())
                .limit(20)
            )
        ).scalars()
        issues.extend(
            DataHealthIssue(
                id=f"incomplete-import:{run.id}",
                category="incomplete_import",
                severity="error" if run.status == "failed" else "warning",
                title="Import is niet volledig verwerkt",
                description=(
                    f"Importstatus: {run.status}. Controleer de "
                    "importdetails en verwerk de bron opnieuw als dat nodig is."
                ),
                impact_count=(
                    int(run.rejected_count or 0) + int(run.skipped_count or 0)
                ),
                source="imports",
                action=action(
                    "view_imports",
                    "/api/v1/connectors/file-uploads/runs",
                    permissions=self._permissions,
                ),
            )
            for run in import_runs
        )
        return issues

    async def _canonical_data_issues(self) -> list[DataHealthIssue]:
        """Detect source-side defects before they reach an exporter."""
        issues: list[DataHealthIssue] = []
        trade_types = ("purchase", "sale")

        incomplete_trades = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Account.name,
                    Transaction.description,
                    Transaction.occurred_at,
                )
                .join(Account, Account.id == Transaction.account_id)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.transaction_type.in_(trade_types),
                    or_(
                        Transaction.quantity.is_(None),
                        Transaction.unit_price.is_(None),
                    ),
                )
                .order_by(Transaction.occurred_at.desc())
                .limit(100)
            )
        ).all()
        if incomplete_trades:
            issues.append(
                DataHealthIssue(
                    id="incomplete-transactions:trade-fields",
                    category="incomplete_transaction",
                    severity="error",
                    title="Transacties missen handelsgegevens",
                    description=(
                        "Koop- of verkooptransacties zonder hoeveelheid of "
                        "stukprijs kunnen niet betrouwbaar worden geëxporteerd."
                    ),
                    impact_count=len(incomplete_trades),
                    source="transactions",
                    details=[
                        self._transaction_detail(row)
                        for row in incomplete_trades[:10]
                    ],
                    affected_transaction_ids=[
                        str(row[0]) for row in incomplete_trades
                    ],
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?status=booked",
                        permissions=self._permissions,
                    ),
                )
            )

        zero_cost_trades = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Account.name,
                    Transaction.description,
                    Transaction.occurred_at,
                )
                .join(Account, Account.id == Transaction.account_id)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.transaction_type.in_(trade_types),
                    or_(
                        Transaction.unit_price == 0,
                        and_(
                            Transaction.amount == 0,
                            Transaction.quantity.is_not(None),
                        ),
                    ),
                )
                .order_by(Transaction.occurred_at.desc())
                .limit(100)
            )
        ).all()
        if zero_cost_trades:
            issues.append(
                DataHealthIssue(
                    id="incomplete-transactions:zero-cost",
                    category="zero_cost_transaction",
                    severity="warning",
                    title="Transacties hebben een nulprijs",
                    description=(
                        "Deze transacties lijken op een corporate action of "
                        "kosteloze toekenning. Classificeer ze als Transfer In "
                        "of leg de verkrijgingsprijs vast."
                    ),
                    impact_count=len(zero_cost_trades),
                    source="transactions",
                    details=[
                        self._transaction_detail(row)
                        for row in zero_cost_trades[:10]
                    ],
                    affected_transaction_ids=[
                        str(row[0]) for row in zero_cost_trades
                    ],
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?type=purchase",
                        permissions=self._permissions,
                    ),
                )
            )

        negative_accounts = (
            await self._session.execute(
                select(Account.id, Account.name, Account.current_balance)
                .where(
                    Account.tenant_id == self._tenant_id,
                    Account.is_active.is_(True),
                    Account.current_balance < 0,
                )
                .order_by(Account.name)
            )
        ).all()
        if negative_accounts:
            issues.append(
                DataHealthIssue(
                    id="negative-balances:accounts",
                    category="negative_balance",
                    severity="warning",
                    title="Accounts hebben een negatief saldo",
                    description=(
                        "Een negatief actueel saldo wijst meestal op een "
                        "ontbrekende storting of Transfer In vóór een aankoop."
                    ),
                    impact_count=len(negative_accounts),
                    source="accounts",
                    details=[
                        f"{row[1]}: {row[2]}" for row in negative_accounts
                    ],
                    action=action(
                        "view_accounts",
                        "/api/v1/accounts",
                        permissions=self._permissions,
                    ),
                )
            )

        transfer_rows = (
            await self._session.execute(
                select(
                    Transaction.account_id,
                    Account.name,
                    Transaction.amount,
                    Transaction.currency_code,
                    func.date(Transaction.occurred_at),
                    Transaction.description,
                )
                .join(Account, Account.id == Transaction.account_id)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.transaction_type == "transfer",
                )
                .order_by(Transaction.occurred_at)
            )
        ).all()
        unmatched_transfers = self._unmatched_transfers(transfer_rows)
        if unmatched_transfers:
            issues.append(
                DataHealthIssue(
                    id="unbalanced-transfers:canonical",
                    category="unbalanced_transfer",
                    severity="error",
                    title="Transfers missen een tegenboeking",
                    description=(
                        "Elke interne transfer moet een uitgaande én inkomende "
                        "kant hebben. Markeer externe geldstromen expliciet."
                    ),
                    impact_count=len(unmatched_transfers),
                    source="transactions",
                    details=[
                        f"{row[1]}: {row[2]} {row[3]} op {row[4]}"
                        for row in unmatched_transfers[:10]
                    ],
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?type=transfer",
                        permissions=self._permissions,
                    ),
                )
            )

        incomplete_holdings = (
            await self._session.execute(
                select(Holding.id, Account.name, Security.name)
                .join(Account, Account.id == Holding.account_id)
                .join(Security, Security.id == Holding.security_id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.quantity != 0,
                    Holding.price.is_(None),
                    Holding.market_value.is_(None),
                )
                .order_by(Holding.observed_at.desc())
                .limit(100)
            )
        ).all()
        if incomplete_holdings:
            issues.append(
                DataHealthIssue(
                    id="incomplete-holdings:valuation",
                    category="incomplete_holding",
                    severity="warning",
                    title="Posities missen waarderingsgegevens",
                    description=(
                        "Een positie mist een prijs of marktwaarde. Daardoor "
                        "kunnen portefeuillewaarde en rendement afwijken."
                    ),
                    impact_count=len(incomplete_holdings),
                    source="holdings",
                    details=[
                        f"{row[2]} in {row[1]}"
                        for row in incomplete_holdings[:10]
                    ],
                    affected_transaction_ids=[],
                    action=action(
                        "view_holdings",
                        "/api/v1/holdings",
                        permissions=self._permissions,
                    ),
                )
            )

        incomplete_securities = (
            await self._session.execute(
                select(Security.id, Security.name)
                .where(
                    Security.isin.is_(None),
                    Security.ticker.is_(None),
                )
                .order_by(Security.name)
                .limit(100)
            )
        ).all()
        if incomplete_securities:
            issues.append(
                DataHealthIssue(
                    id="incomplete-securities:identity",
                    category="incomplete_security_identity",
                    severity="error",
                    title="Securities missen een identificatie",
                    description=(
                        "Zonder ISIN of ticker kunnen koersproviders en "
                        "exportbestemmingen de security niet betrouwbaar "
                        "herkennen."
                    ),
                    impact_count=len(incomplete_securities),
                    source="securities",
                    details=[str(row[1]) for row in incomplete_securities[:10]],
                    action=action(
                        "view_holdings",
                        "/api/v1/holdings",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    @staticmethod
    def _transaction_detail(row: Iterable[object]) -> str:
        _transaction_id, account_name, description, occurred_at = row
        occurred_at = cast("datetime | None", occurred_at)
        date = (
            occurred_at.strftime("%Y-%m-%d")
            if occurred_at
            else "onbekende datum"
        )
        return f"{account_name} · {date} · {description or 'Transactie'}"

    @staticmethod
    def _unmatched_transfers(
        rows: Sequence[Iterable[object]],
    ) -> list[tuple[object, ...]]:
        """Pair opposite transfers by date, currency and absolute amount."""
        unmatched: list[tuple[object, ...]] = []
        # SQLAlchemy returns Row objects here; normalize them once so the
        # pairing logic remains independent of the session implementation.
        available = [tuple(row) for row in rows]
        normalized_rows = list(available)
        for row in normalized_rows:
            if row not in available:
                continue
            available.remove(row)
            match_index = next(
                (
                    index
                    for index, candidate in enumerate(available)
                    if candidate[0] != row[0]
                    and candidate[2] == -cast("Decimal", row[2])
                    and candidate[3] == row[3]
                    and candidate[4] == row[4]
                ),
                None,
            )
            if match_index is None:
                unmatched.append(row)
            else:
                available.pop(match_index)
        return unmatched

    async def _changed_provider_issues(
        self, sources: list[DataHealthSource]
    ) -> list[DataHealthIssue]:
        """Create recovery actions for revised provider transactions."""
        rows = (
            await self._session.execute(
                select(Transaction.provider_key, func.count(Transaction.id))
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.revision > 1,
                )
                .group_by(Transaction.provider_key)
            )
        ).all()
        issues: list[DataHealthIssue] = []
        for provider, count in rows:
            source = next(
                (item for item in sources if item.provider == str(provider)),
                None,
            )
            recovery = (
                action(
                    "sync_connection",
                    f"/api/v1/sync/connections/{source.id}/start",
                    permissions=self._permissions,
                )
                if source is not None
                else action(
                    "view_transactions",
                    "/api/v1/transactions",
                    permissions=self._permissions,
                )
            )
            issues.append(
                DataHealthIssue(
                    id=f"provider-data-changed:{provider}",
                    category="provider_data_changed",
                    severity="warning",
                    title="Providerdata is gewijzigd",
                    description=(
                        f"{count} transacties van {provider} zijn door de bron "
                        "aangepast na een eerdere synchronisatie."
                    ),
                    impact_count=int(count),
                    provider=str(provider),
                    source="transactions",
                    action=recovery,
                )
            )
        return issues

    @staticmethod
    def _status(
        control_status: str,
        quality_status: str,
        issues: list[DataHealthIssue] | None = None,
    ) -> DataHealthStatus:
        if control_status == "sync_failed":
            return "error"
        if control_status == "attention_required":
            return "attention_required"
        if control_status == "partial":
            return "partial"
        if quality_status == "attention_required":
            return "attention_required"
        if quality_status == "unavailable":
            return "unavailable"
        if any(issue.severity == "error" for issue in issues or []):
            return "error"
        if any(issue.severity == "warning" for issue in issues or []):
            return "attention_required"
        return "healthy"

    @staticmethod
    def _issue(issue: ControlPlaneIssue) -> DataHealthIssue:
        category = {
            "security_mapping": "unresolved_security",
            "freshness": "stale_prices",
            "export": "failed_export",
            "data_quality": "reconciliation",
        }.get(issue.category, issue.category)
        return DataHealthIssue(
            id=issue.id,
            category=category,  # type: ignore[arg-type]
            severity=issue.severity,
            title=issue.title,
            description=issue.description,
            impact_count=issue.impact_count,
            provider=issue.provider,
            source=issue.category,
            action=issue.action,
            affected_transaction_ids=issue.affected_transaction_ids,
        )
