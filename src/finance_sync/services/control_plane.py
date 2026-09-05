"""Tenant-scoped aggregation service for the operational control plane."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from finance_sync.control_plane_contract import (
    latest_timestamp,
    overview_status,
)
from finance_sync.exporter.models import ExportRun
from finance_sync.models import (
    Account,
    Credential,
    EnrichmentFreshness,
    ExportTarget,
    Holding,
    Security,
    SyncRun,
    SyncSchedule,
    UnresolvedSecurity,
)
from finance_sync.models.reconciliation import (
    ReconciliationResult,
    ReconciliationRun,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
from finance_sync.schemas.control_plane import (
    ControlPlaneConnection,
    ControlPlaneCoverage,
    ControlPlaneDestination,
    ControlPlaneFreshness,
    ControlPlaneIssue,
    ControlPlaneOverview,
    ControlPlaneSummary,
    ControlPlaneSync,
    InstallationStatus,
)
from finance_sync.services.control_plane_actions import action
from finance_sync.utils.redaction import sanitize_error


class ControlPlaneService:
    """Build a read-only overview from existing persistence models."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: str,
        *,
        permissions: set[str] | None = None,
        freshness_limit: timedelta = timedelta(hours=24),
        redis_configured: bool = False,
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._freshness_limit = freshness_limit
        self._permissions = permissions
        self._redis_configured = redis_configured
        self._now = now or datetime.now(UTC)

    async def get_overview(self) -> ControlPlaneOverview:
        credentials = list(
            (
                await self._session.execute(
                    select(Credential)
                    .where(Credential.tenant_id == self._tenant_id)
                    .order_by(Credential.created_at)
                )
            ).scalars()
        )
        connection_ids = [str(row.id) for row in credentials]
        schedules = await self._load_schedules(connection_ids)
        sync_rows = await self._load_syncs(connection_ids)
        connections = [
            self._connection(row, schedules.get(str(row.id)), self._permissions)
            for row in credentials
        ]
        syncs = [self._sync(row, self._permissions) for row in sync_rows]
        issues = self._connection_issues(connections, syncs)
        issues.extend(await self._security_issues(credentials))
        issues.extend(await self._reconciliation_issues())
        freshness = await self._freshness()
        freshness.ingestion_last_at = latest_timestamp(
            [
                *(row.last_success_at for row in connections),
                *(row.last_attempt_at for row in connections),
            ]
        )
        destinations = await self._destinations()
        issues.extend(self._freshness_issues(freshness))
        issues.extend(self._destination_issues(destinations))
        coverage = await self._coverage(credentials)

        failed_syncs = sum(1 for row in syncs if row.status == "failed")
        failed_destinations = sum(
            1
            for row in destinations
            if row.status in {"failed", "error"}
            or row.health_status in {"failed", "error", "unhealthy"}
        )
        status = overview_status(
            failed_syncs=failed_syncs,
            issues_open=len(issues),
            failed_destinations=failed_destinations,
            freshness_status=freshness.status,
        )

        timestamps = [
            *(row.last_attempt_at for row in connections),
            *(row.last_success_at for row in connections),
            *(row.started_at for row in syncs),
            *(row.completed_at for row in syncs),
            freshness.last_enrichment_at,
            freshness.ingestion_last_at,
            freshness.market_data_last_at,
            *(row.last_checked_at for row in destinations),
            *(row.last_export_at for row in destinations),
            await self._latest_reconciliation_at(),
        ]
        as_of = latest_timestamp(timestamps)
        return ControlPlaneOverview(
            status=status,
            installation=InstallationStatus(
                redis=(
                    "configured" if self._redis_configured else "not_configured"
                )
            ),
            summary=ControlPlaneSummary(
                connections_total=len(connections),
                connections_healthy=sum(
                    1 for row in connections if row.status == "healthy"
                ),
                syncs_failed=failed_syncs,
                issues_open=len(issues),
                destinations_failed=failed_destinations,
            ),
            connections=connections,
            syncs=syncs,
            issues=issues,
            freshness=freshness,
            coverage=coverage,
            destinations=destinations,
            as_of=as_of,
            generated_at=self._now,
        )

    async def _load_schedules(
        self, connection_ids: list[str]
    ) -> dict[str, SyncSchedule]:
        if not connection_ids:
            return {}
        rows = (
            await self._session.execute(
                select(SyncSchedule).where(
                    SyncSchedule.tenant_id == self._tenant_id,
                    SyncSchedule.scope == "ingestion",
                    SyncSchedule.target_id.in_(connection_ids),
                )
            )
        ).scalars()
        return {str(row.target_id): row for row in rows}

    async def _load_syncs(self, connection_ids: list[str]) -> list[SyncRun]:
        if not connection_ids:
            return []
        rows = (
            await self._session.execute(
                select(SyncRun)
                .join(
                    Credential,
                    Credential.id == SyncRun.connection_id,
                )
                .where(Credential.id.in_(connection_ids))
                .where(Credential.tenant_id == self._tenant_id)
                .order_by(SyncRun.started_at.desc())
                .limit(100)
            )
        ).scalars()
        return list(rows)

    @staticmethod
    def _label(row: Credential) -> str:
        if row.description:
            try:
                value = json.loads(row.description).get("_label")
                if isinstance(value, str) and value.strip():
                    return value
            except (TypeError, ValueError):
                pass
        return row.provider_key

    @staticmethod
    def _connection(
        row: Credential,
        schedule: SyncSchedule | None,
        permissions: set[str] | None = None,
    ) -> ControlPlaneConnection:
        file_import_pending = row.provider_key in {
            "degiro_pension",
            "saxo_investor",
        } and not bool(getattr(row, "encrypted_payload", None))
        status = (
            "paused"
            if row.status == "paused"
            else (
                "error"
                if row.last_error and not file_import_pending
                else ("healthy" if row.last_success_at else "pending")
            )
        )
        return ControlPlaneConnection(
            id=str(row.id),
            provider=row.provider_key,
            name=ControlPlaneService._label(row),
            status=status,
            last_attempt_at=row.last_attempt_at,
            last_success_at=row.last_success_at,
            last_error=(
                None
                if file_import_pending
                else sanitize_error(row.last_error or "") or None
            ),
            last_error_category=(
                None
                if file_import_pending
                else getattr(row, "last_error_category", None)
            ),
            last_test_at=getattr(row, "last_test_at", None),
            last_test_status=getattr(row, "last_test_status", None),
            last_test_error=getattr(row, "last_test_error", None),
            next_scheduled_at=schedule.next_run_at if schedule else None,
            actions=[
                action(
                    "test_connection",
                    f"/api/v1/connectors/configs/{row.id}/test",
                    permissions=permissions,
                ),
                action(
                    "sync_connection",
                    f"/api/v1/sync/connections/{row.id}/start",
                    permissions=permissions,
                    disabled_reason=(
                        "De verbinding is gepauzeerd."
                        if row.status == "paused"
                        else None
                    ),
                ),
                action(
                    "view_connection",
                    f"/api/v1/connectors/configs/{row.id}",
                    permissions=permissions,
                ),
                action(
                    "edit_connection",
                    f"/api/v1/connectors/configs/{row.id}",
                    permissions=permissions,
                ),
                action(
                    "pause_connection",
                    f"/api/v1/connectors/configs/{row.id}/pause",
                    permissions=permissions,
                    disabled_reason=(
                        "De verbinding is al gepauzeerd."
                        if row.status == "paused"
                        else None
                    ),
                ),
            ],
        )

    @staticmethod
    def _sync(
        row: SyncRun, permissions: set[str] | None = None
    ) -> ControlPlaneSync:
        return ControlPlaneSync(
            id=str(row.id),
            connector=row.connector,
            connection_id=str(row.connection_id) if row.connection_id else None,
            status=str(row.status),
            started_at=row.started_at,
            completed_at=row.completed_at,
            items_processed=row.items_processed,
            error_message=sanitize_error(row.error_message or "") or None,
            error_category=row.error_category,
            cursor=row.cursor,
            actions=[
                action(
                    "view_sync_run",
                    f"/api/v1/sync-runs/{row.id}",
                    permissions=permissions,
                ),
                action(
                    "retry_sync",
                    f"/api/v1/sync-runs/{row.id}/retry",
                    permissions=permissions,
                    disabled_reason=(
                        "Alleen mislukte runs kunnen opnieuw worden geprobeerd."
                        if str(row.status) != "failed"
                        else None
                    ),
                ),
            ],
        )

    def _connection_issues(
        self,
        connections: list[ControlPlaneConnection],
        syncs: list[ControlPlaneSync],
    ) -> list[ControlPlaneIssue]:
        issues: list[ControlPlaneIssue] = []
        issues.extend(
            [
                ControlPlaneIssue(
                    id=f"connection-error:{row.id}",
                    severity="error",
                    category="connection",
                    title="Verbinding vereist aandacht",
                    description=(
                        row.last_error or "De laatste poging is mislukt."
                    ),
                    action=action(
                        "edit_connection",
                        f"/api/v1/connectors/configs/{row.id}",
                        permissions=self._permissions,
                    ),
                )
                for row in connections
                if row.status == "error"
            ]
        )
        issues.extend(
            [
                ControlPlaneIssue(
                    id=f"sync-failed:{row.id}",
                    severity="error",
                    category="synchronization",
                    title="Synchronisatie mislukt",
                    description=(
                        row.error_message or "De synchronisatie is mislukt."
                    ),
                    action=action(
                        "view_sync_run",
                        f"/api/v1/sync-runs/{row.id}",
                        permissions=self._permissions,
                    ),
                )
                for row in syncs
                if row.status == "failed"
            ]
        )
        return issues

    async def _security_issues(
        self, credentials: list[Credential]
    ) -> list[ControlPlaneIssue]:
        providers = {row.provider_key for row in credentials}
        if not providers:
            return []
        rows = (
            await self._session.execute(
                select(UnresolvedSecurity).where(
                    UnresolvedSecurity.tenant_id == self._tenant_id,
                    UnresolvedSecurity.provider_key.in_(providers),
                    UnresolvedSecurity.resolved_security_id.is_(None),
                )
            )
        ).scalars()
        issues: list[ControlPlaneIssue] = []
        for row in rows:
            candidates = await self._security_candidates(row)
            issues.append(
                ControlPlaneIssue(
                    id=f"security-unresolved:{row.id}",
                    severity="warning",
                    category="security_mapping",
                    title="Security niet herkend",
                    description=(
                        "Een geïmporteerde positie kan niet worden gekoppeld."
                    ),
                    action=action(
                        "map_security",
                        "/api/v1/securities/map",
                        permissions=self._permissions,
                    ),
                    provider=row.provider_key,
                    external_record_id=row.external_security_id,
                    # This is one unresolved provider identity.  Counting
                    # every transaction/holding from the provider here made
                    # one missing mapping appear as thousands of separate
                    # issues (for example 2695 for Trading212).
                    impact_count=1,
                    candidate_securities=candidates,
                    confidence=(
                        candidates[0]["confidence"] if candidates else None
                    ),
                )
            )
        return issues

    async def _security_candidates(
        self, row: UnresolvedSecurity
    ) -> list[dict[str, Any]]:
        predicates: list[Any] = []
        for column, value in (
            (Security.isin, row.raw_isin),
            (Security.figi, row.raw_figi),
            (Security.ticker, row.raw_ticker),
        ):
            if value:
                predicates.append(column == value)
        if row.raw_name:
            predicates.append(Security.name.ilike(f"%{row.raw_name[:80]}%"))
        if not predicates:
            return []
        candidates = list(
            (
                await self._session.execute(
                    select(Security).where(or_(*predicates)).limit(5)
                )
            ).scalars()
        )
        return [
            {
                "security_id": str(candidate.id),
                "name": candidate.name,
                "ticker": candidate.ticker,
                "isin": candidate.isin,
                "confidence": (
                    "high"
                    if (row.raw_isin and candidate.isin == row.raw_isin)
                    or (row.raw_figi and candidate.figi == row.raw_figi)
                    or (row.raw_ticker and candidate.ticker == row.raw_ticker)
                    else "medium"
                ),
            }
            for candidate in candidates
        ]

    async def _reconciliation_issues(self) -> list[ControlPlaneIssue]:
        latest = await self._session.scalar(
            select(ReconciliationRun)
            .where(ReconciliationRun.tenant_id == self._tenant_id)
            .order_by(ReconciliationRun.started_at.desc())
            .limit(1)
        )
        if latest is None:
            return []
        results = list(
            (
                await self._session.execute(
                    select(ReconciliationResult).where(
                        ReconciliationResult.tenant_id == self._tenant_id,
                        ReconciliationResult.run_id == latest.id,
                    )
                )
            ).scalars()
        )
        return [
            ControlPlaneIssue(
                id=f"reconciliation:{result.id}",
                severity=str(result.severity),
                category="data_quality",
                title="Datakwaliteitscontrole vereist aandacht",
                description=result.description or "Controleer de finding.",
                action=action(
                    "view_reconciliation",
                    f"/api/v1/reconciliation/{latest.id}",
                    permissions=self._permissions,
                ),
                provider=result.provider_key,
                impact_count=sum(
                    1
                    for value in (
                        result.transaction_id_a,
                        result.transaction_id_b,
                    )
                    if value
                ),
                affected_transaction_ids=[
                    str(value)
                    for value in (
                        result.transaction_id_a,
                        result.transaction_id_b,
                    )
                    if value
                ],
            )
            for result in results
        ]

    def _freshness_issues(
        self,
        freshness: ControlPlaneFreshness,
    ) -> list[ControlPlaneIssue]:
        if (
            freshness.status == "fresh"
            and freshness.holdings_without_valuation == 0
        ):
            return []
        return [
            ControlPlaneIssue(
                id="freshness:quotes",
                severity="warning",
                category="freshness",
                title="Koersen zijn niet volledig actueel",
                description=(
                    f"{freshness.securities_stale} securities zijn stale en "
                    f"{freshness.securities_without_quote} hebben geen "
                    f"actuele koers; {freshness.holdings_without_valuation} "
                    "holdings hebben geen waardering."
                ),
                action=action(
                    "refresh_quotes",
                    "/api/v1/enrichment/refresh-quotes",
                    permissions=self._permissions,
                ),
            )
        ]

    def _destination_issues(
        self,
        destinations: list[ControlPlaneDestination],
    ) -> list[ControlPlaneIssue]:
        health_issues = [
            ControlPlaneIssue(
                id=f"destination-health:{row.id}",
                severity="error",
                category="destination",
                title="Bestemming is niet gezond",
                description=row.last_error
                or "De laatste health check is mislukt.",
                action=action(
                    "test_destination",
                    f"/api/v1/destinations/{row.id}/test",
                    permissions=self._permissions,
                ),
            )
            for row in destinations
            if row.status in {"failed", "error"}
            or row.health_status in {"failed", "error", "unhealthy"}
        ]
        export_issues = [
            ControlPlaneIssue(
                id=f"export-failed:{row.id}",
                severity="error",
                category="export",
                title="Export mislukt",
                description=(
                    row.last_export_error
                    or f"{row.failed_export_count} export(s) voor {row.name} "
                    "zijn mislukt."
                ),
                action=next(
                    (
                        candidate
                        for candidate in row.actions
                        if candidate.key == "retry_export"
                    ),
                    action(
                        "test_destination",
                        f"/api/v1/destinations/{row.id}/test",
                        permissions=self._permissions,
                    ),
                ),
            )
            for row in destinations
            if row.failed_export_count > 0
        ]
        return health_issues + export_issues

    async def _freshness(self) -> ControlPlaneFreshness:
        security_ids = (
            select(Holding.security_id)
            .where(Holding.tenant_id == self._tenant_id)
            .distinct()
        )
        rows = list(
            (
                await self._session.execute(
                    select(EnrichmentFreshness).where(
                        EnrichmentFreshness.security_id.in_(security_ids)
                    )
                )
            ).scalars()
        )
        total = await self._session.scalar(
            select(func.count(func.distinct(Holding.security_id))).where(
                Holding.tenant_id == self._tenant_id
            )
        )
        total_count = int(total or 0)
        holdings_without_valuation = int(
            await self._session.scalar(
                select(func.count(Holding.id))
                .outerjoin(
                    EnrichmentFreshness,
                    EnrichmentFreshness.security_id == Holding.security_id,
                )
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.market_value.is_(None),
                    (EnrichmentFreshness.status.is_(None))
                    | (EnrichmentFreshness.status != "unavailable_accepted"),
                )
            )
            or 0
        )
        accepted_ids = {
            str(getattr(row, "security_id", ""))
            for row in rows
            if getattr(row, "status", None) == "unavailable_accepted"
        }
        active_rows = [
            row
            for row in rows
            if getattr(row, "status", None) != "unavailable_accepted"
        ]
        total_count = max(total_count - len(accepted_ids), 0)
        cutoff = self._now - self._freshness_limit
        fresh = sum(
            1
            for row in active_rows
            if row.last_quote_fetch and row.last_quote_fetch >= cutoff
        )
        stale = sum(
            1
            for row in active_rows
            if row.last_quote_fetch and row.last_quote_fetch < cutoff
        )
        without_quote = max(total_count - len(active_rows), 0) + sum(
            1
            for row in active_rows
            if row.last_quote_fetch is None
        )
        latest = max((row.updated_at for row in rows), default=None)
        by_source: dict[str, dict[str, int]] = {}
        by_category: dict[str, dict[str, int]] = {}
        for row in rows:
            source = str(row.data_source or "unknown")
            bucket = by_source.setdefault(
                source, {"total": 0, "fresh": 0, "stale": 0, "without_quote": 0}
            )
            bucket["total"] += 1
            if row.last_quote_fetch is None:
                bucket["without_quote"] += 1
            elif row.last_quote_fetch >= cutoff:
                bucket["fresh"] += 1
            else:
                bucket["stale"] += 1
            for category, timestamp in (
                ("metadata", getattr(row, "last_metadata_fetch", None)),
                ("quote", row.last_quote_fetch),
                (
                    "daily_price",
                    getattr(row, "last_daily_price_fetch", None),
                ),
                (
                    "intraday_price",
                    getattr(row, "last_intraday_price_fetch", None),
                ),
            ):
                category_bucket = by_category.setdefault(
                    category,
                    {"total": 0, "fresh": 0, "stale": 0, "unavailable": 0},
                )
                category_bucket["total"] += 1
                if timestamp is None:
                    category_bucket["unavailable"] += 1
                elif timestamp >= cutoff:
                    category_bucket["fresh"] += 1
                else:
                    category_bucket["stale"] += 1
        status = (
            "fresh"
            if (total_count and fresh == total_count)
            or (not total_count and accepted_ids)
            else (
                "unavailable"
                if not total_count
                else ("partial" if fresh else "stale")
            )
        )
        return ControlPlaneFreshness(
            status=status,
            securities_total=total_count,
            securities_fresh=fresh,
            securities_stale=stale,
            securities_without_quote=without_quote,
            holdings_without_valuation=holdings_without_valuation,
            by_source=by_source,
            by_category=by_category,
            market_data_last_at=max(
                (
                    value
                    for row in rows
                    for value in (
                        row.last_quote_fetch,
                        getattr(row, "last_daily_price_fetch", None),
                        getattr(row, "last_intraday_price_fetch", None),
                    )
                    if value is not None
                ),
                default=None,
            ),
            last_enrichment_at=latest,
        )

    async def _latest_reconciliation_at(self) -> datetime | None:
        return await self._session.scalar(
            select(func.max(ReconciliationRun.completed_at)).where(
                ReconciliationRun.tenant_id == self._tenant_id
            )
        )

    async def _coverage(
        self, credentials: list[Credential]
    ) -> ControlPlaneCoverage:
        connection_ids = {str(row.id) for row in credentials}
        rows = (
            await self._session.execute(
                select(Account.connection_id, Account.provider_key)
                .where(
                    Account.tenant_id == self._tenant_id,
                    Account.connection_id.in_(connection_ids),
                )
                .distinct()
            )
        ).all()
        return ControlPlaneCoverage(
            connections_with_data=len({str(row[0]) for row in rows if row[0]}),
            connections_total=len(credentials),
            providers=sorted({str(row[1]) for row in rows}),
        )

    async def _destinations(self) -> list[ControlPlaneDestination]:
        rows = list(
            (
                await self._session.execute(
                    select(ExportTarget).where(
                        ExportTarget.tenant_id == self._tenant_id
                    )
                )
            ).scalars()
        )
        schedule_ids = {row.schedule_id for row in rows if row.schedule_id}
        schedules = {}
        if schedule_ids:
            schedules = {
                str(row.id): row
                for row in (
                    await self._session.execute(
                        select(SyncSchedule).where(
                            SyncSchedule.tenant_id == self._tenant_id,
                            SyncSchedule.id.in_(schedule_ids),
                        )
                    )
                ).scalars()
            }
        destination_rows: list[ControlPlaneDestination] = []
        for row in rows:
            latest_export = await self._session.scalar(
                select(ExportRun)
                .where(
                    ExportRun.tenant_id == self._tenant_id,
                    ExportRun.target_id == str(row.id),
                )
                .order_by(ExportRun.started_at.desc())
                .limit(1)
            )
            latest_failed_export = await self._session.scalar(
                select(ExportRun)
                .where(
                    ExportRun.tenant_id == self._tenant_id,
                    ExportRun.target_id == str(row.id),
                    ExportRun.status == "failed",
                )
                .order_by(ExportRun.started_at.desc())
                .limit(1)
            )
            failed_count = int(
                await self._session.scalar(
                    select(func.count(ExportRun.id)).where(
                        ExportRun.tenant_id == self._tenant_id,
                        ExportRun.target_id == str(row.id),
                        ExportRun.status == "failed",
                    )
                )
                or 0
            )
            destination_rows.append(
                ControlPlaneDestination(
                    id=str(row.id),
                    type=row.target_type,
                    name=row.display_name,
                    status=row.status,
                    health_status=row.last_health_status,
                    last_checked_at=row.last_checked_at,
                    last_error=(
                        sanitize_error(row.last_health_error or "") or None
                    ),
                    next_scheduled_at=(
                        schedules[str(row.schedule_id)].next_run_at
                        if row.schedule_id and str(row.schedule_id) in schedules
                        else None
                    ),
                    selected_account_ids=list(row.selected_account_ids or []),
                    last_export_status=(
                        latest_export.status
                        if latest_export is not None
                        else None
                    ),
                    last_export_at=(
                        (latest_export.completed_at or latest_export.started_at)
                        if latest_export is not None
                        else None
                    ),
                    last_export_error=(
                        sanitize_error(
                            getattr(latest_export, "error_message", "") or ""
                        )
                        or None
                        if latest_export is not None
                        else None
                    ),
                    failed_export_count=failed_count,
                    delivery_checkpoint=(
                        getattr(latest_export, "delivery_checkpoint", None)
                        if latest_export is not None
                        else None
                    ),
                    actions=[
                        action(
                            "test_destination",
                            f"/api/v1/destinations/{row.id}/test",
                            permissions=self._permissions,
                        ),
                        action(
                            "run_export",
                            f"/api/v1/destinations/{row.id}/run",
                            permissions=self._permissions,
                            disabled_reason=(
                                "Activeer de bestemming voordat je exporteert."
                                if row.status != "active"
                                else None
                            ),
                        ),
                        action(
                            "preview_destination",
                            f"/api/v1/destinations/{row.id}/preview",
                            permissions=self._permissions,
                        ),
                        action(
                            "configure_destination",
                            f"/api/v1/destinations/{row.id}",
                            permissions=self._permissions,
                        ),
                        action(
                            "pause_destination",
                            f"/api/v1/destinations/{row.id}/pause",
                            permissions=self._permissions,
                            disabled_reason=(
                                "De bestemming is al gepauzeerd."
                                if row.status == "paused"
                                else None
                            ),
                        ),
                        *(
                            [
                                action(
                                    "retry_export",
                                    f"/api/v1/destinations/{row.id}/retry",
                                    permissions=self._permissions,
                                )
                            ]
                            if latest_failed_export is not None
                            else []
                        ),
                    ],
                )
            )
        return destination_rows
