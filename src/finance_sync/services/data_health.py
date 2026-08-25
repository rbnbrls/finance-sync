"""Canonical aggregation for the user-facing Data health workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from finance_sync.models import Account, ImportRun, Transaction
from finance_sync.schemas.data_health import (
    DataHealthIssue,
    DataHealthOverview,
    DataHealthReconciliation,
    DataHealthSource,
)
from finance_sync.services.control_plane import ControlPlaneService
from finance_sync.services.control_plane_actions import action
from finance_sync.services.data_quality import DataQualityService

if TYPE_CHECKING:
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
            status=self._status(control.status, quality.status),
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
            if source.status != "healthy" or source.transactions:
                continue
            sync_action = next(
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
            issues.append(
                DataHealthIssue(
                    id=f"missing-transactions:{source.id}",
                    category="missing_transactions",
                    severity="warning",
                    title="Bron bevat nog geen transacties",
                    description=(
                        f"De gezonde bron {source.provider} heeft nog geen "
                        "transacties in de canonieke dataset."
                    ),
                    provider=source.provider,
                    source="transactions",
                    action=sync_action,
                )
            )
        return issues

    async def _additional_issues(self) -> list[DataHealthIssue]:
        """Detect account conflicts and incomplete imports."""
        issues: list[DataHealthIssue] = []

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
                    f"/api/v1/sync/connections/{source.id}",
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
    def _status(control_status: str, quality_status: str) -> str:
        if control_status == "sync_failed":
            return "error"
        if control_status in {"attention_required", "partial"}:
            return control_status
        if quality_status == "attention_required":
            return "attention_required"
        if quality_status == "unavailable":
            return "unavailable"
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
        )
