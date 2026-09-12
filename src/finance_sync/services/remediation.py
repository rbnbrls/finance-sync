"""Tenant-scoped application service for the remediation backlog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update

from finance_sync.models.remediation import DataQualityRemediationItem
from finance_sync.reconciliation.remediation.backlog import (
    BacklogRepository,
    DetectedIssue,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class RemediationService:
    def __init__(self, session: AsyncSession, tenant_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.backlog = BacklogRepository(session)

    async def register_detected_issues(
        self, issues: list[DetectedIssue]
    ) -> list[DataQualityRemediationItem]:
        result: list[DataQualityRemediationItem] = []
        for issue in issues:
            if issue.tenant_id != self.tenant_id:
                message = "detected issue tenant does not match service tenant"
                raise ValueError(message)
            result.append(await self.backlog.register(issue))
        return result

    async def list(self, **filters: Any) -> list[DataQualityRemediationItem]:
        return await self.backlog.list_for_tenant(self.tenant_id, **filters)

    async def get(self, item_id: str) -> DataQualityRemediationItem | None:
        return await self.backlog.get(self.tenant_id, item_id)

    async def requeue(
        self,
        item_id: str,
        *,
        actor_user_id: str | None = None,
        actor_role: str | None = None,
    ) -> bool:
        changed = await self.backlog.transition(
            self.tenant_id,
            item_id,
            status="pending",
            allowed_from=("failed", "manual_review", "deferred", "retry_wait"),
            error=None,
            error_category=None,
            next_attempt_at=datetime.now(UTC),
        )
        if changed:
            await self._audit(
                action="retry",
                item_id=item_id,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
            )
        return changed

    async def bulk_requeue(
        self,
        item_ids: list[str],
        *,
        actor_user_id: str | None = None,
        actor_role: str | None = None,
    ) -> list[str]:
        """Requeue at most the caller-provided bounded set, tenant-scoped."""
        bounded = list(dict.fromkeys(item_ids))[:100]
        if not bounded:
            return []
        result = await self.session.execute(
            update(DataQualityRemediationItem)
            .where(
                DataQualityRemediationItem.tenant_id == self.tenant_id,
                DataQualityRemediationItem.id.in_(bounded),
                DataQualityRemediationItem.status.in_(
                    ("failed", "manual_review", "deferred", "retry_wait")
                ),
            )
            .values(
                status="pending",
                next_attempt_at=datetime.now(UTC),
                last_error=None,
                last_error_category=None,
            )
            .returning(
                DataQualityRemediationItem.id,
                DataQualityRemediationItem.provider_key,
            )
        )
        rows = result.all()
        if rows:
            from finance_sync.services.connection_audit import (
                log_connection_event,
            )

            await log_connection_event(
                self.session,
                tenant_id=self.tenant_id,
                provider_key="remediation",
                action="retry",
                detail={
                    "item_ids": [str(row[0]) for row in rows],
                    "count": len(rows),
                },
                actor_user_id=actor_user_id,
                actor_role=actor_role,
            )
        return [str(row[0]) for row in rows]

    async def ignore(
        self,
        item_id: str,
        reason: str,
        *,
        actor_user_id: str | None = None,
        actor_role: str | None = None,
    ) -> bool:
        changed = await self.backlog.transition(
            self.tenant_id,
            item_id,
            status="ignored",
            allowed_from=(
                "pending",
                "processing",
                "deferred",
                "retry_wait",
                "failed",
                "manual_review",
            ),
            error=reason,
            error_category="operator",
        )
        if changed:
            await self._audit(
                action="ignore",
                item_id=item_id,
                reason=reason,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
            )
        return changed

    async def patch(
        self,
        item_id: str,
        *,
        priority: int | None = None,
        status: str | None = None,
        actor_user_id: str | None = None,
        actor_role: str | None = None,
    ) -> bool:
        values: dict[str, object] = {}
        if priority is not None:
            values["priority"] = priority
        if status is not None:
            values["status"] = status
        if not values:
            return False
        result = await self.session.execute(
            update(DataQualityRemediationItem)
            .where(
                DataQualityRemediationItem.tenant_id == self.tenant_id,
                DataQualityRemediationItem.id == item_id,
            )
            .values(**values)
        )
        changed = bool(cast("Any", result).rowcount)
        if changed:
            await self._audit(
                action="update",
                item_id=item_id,
                detail={
                    key: value
                    for key, value in values.items()
                    if key in {"priority", "status"}
                },
                actor_user_id=actor_user_id,
                actor_role=actor_role,
            )
        return changed

    async def _audit(
        self,
        *,
        action: str,
        item_id: str,
        actor_user_id: str | None,
        actor_role: str | None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        from finance_sync.services.connection_audit import log_connection_event

        payload = {"item_id": item_id, **(detail or {})}
        if reason is not None:
            payload["reason"] = reason
        await log_connection_event(
            self.session,
            tenant_id=self.tenant_id,
            provider_key="remediation",
            action=action,
            detail=payload,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
        )

    async def enqueue_data_health_repairs(
        self,
    ) -> list[DataQualityRemediationItem]:
        """Enqueue safe repair work without calling providers."""
        from finance_sync.connectors.registry import ConnectorRegistry
        from finance_sync.enrichment.identifiers import quote_identifier
        from finance_sync.models import (
            Account,
            Credential,
            EnrichmentFreshness,
            Holding,
            Security,
        )
        from finance_sync.models.unresolved_security import UnresolvedSecurity

        rows = list(
            (
                await self.session.execute(
                    select(UnresolvedSecurity).where(
                        UnresolvedSecurity.tenant_id == self.tenant_id,
                        UnresolvedSecurity.resolved_security_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        issues = [
            DetectedIssue(
                tenant_id=self.tenant_id,
                provider_key=row.provider_key,
                issue_type="unresolved_security",
                affected_entity_type="unresolved_security",
                affected_entity_id=str(row.id),
                severity="warning",
                remediation_strategy="trading212_instrument_metadata"
                if row.provider_key == "trading212"
                else "unsupported",
                context={
                    "external_security_id": row.external_security_id,
                    "raw_ticker": row.raw_ticker,
                },
            )
            for row in rows
        ]
        connector_catalog = ConnectorRegistry().list_connectors()
        api_holding_providers = {
            provider
            for provider, metadata in connector_catalog.items()
            if "api" in metadata.get("ingestion_methods", [])
            and "holdings" in metadata.get("supported_resources", [])
        }
        target_rows = (
            await self.session.execute(
                select(
                    Security,
                    Account.provider_key,
                    Account.connection_id,
                    Account.external_account_id,
                )
                .join(Holding, Holding.security_id == Security.id)
                .join(Account, Account.id == Holding.account_id)
                .join(
                    Credential,
                    (Credential.id == Account.connection_id)
                    & (Credential.status == "active"),
                )
                .outerjoin(
                    EnrichmentFreshness,
                    EnrichmentFreshness.security_id == Security.id,
                )
                .where(
                    Holding.tenant_id == self.tenant_id,
                    EnrichmentFreshness.last_daily_price_fetch.is_(None),
                    Account.provider_key.in_(api_holding_providers),
                )
                .distinct()
            )
        ).all()
        securities: dict[str, tuple[Any, str, str, str]] = {}
        for (
            security,
            provider,
            connection_id,
            external_account_id,
        ) in target_rows:
            if connection_id is not None:
                securities.setdefault(
                    str(security.id),
                    (
                        security,
                        str(provider),
                        str(connection_id),
                        str(external_account_id),
                    ),
                )
        for (
            security,
            provider,
            connection_id,
            external_account_id,
        ) in securities.values():
            identifier, identifier_type = quote_identifier(security)
            if not identifier:
                continue
            price_end = datetime.now(UTC)
            price_start = price_end - timedelta(days=365)
            issues.append(
                DetectedIssue(
                    tenant_id=self.tenant_id,
                    provider_key=provider,
                    connection_id=connection_id,
                    issue_type="historical_price_gap",
                    affected_entity_type="security",
                    affected_entity_id=str(security.id),
                    severity="warning",
                    # Holdings APIs provide a current quote, not a historical
                    # series; keep this finding manual until a provider
                    # declares a historical-price remediation strategy.
                    remediation_strategy="unsupported",
                    context={
                        "security_id": str(security.id),
                        "identifier": identifier,
                        "identifier_type": identifier_type,
                        "interval": "1d",
                        "start_date": price_start.isoformat(),
                        "end_date": price_end.isoformat(),
                        "minimum_observations": 1,
                        "provider_account_id": external_account_id,
                    },
                )
            )
        quote_securities = list(
            (
                await self.session.execute(
                    select(Security)
                    .join(Holding, Holding.security_id == Security.id)
                    .where(
                        Holding.tenant_id == self.tenant_id,
                        Holding.quantity != 0,
                        Holding.price.is_(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        for security in quote_securities:
            security_context = securities.get(str(security.id))
            if security_context is None:
                continue
            _, provider, connection_id, external_account_id = security_context
            identifier, identifier_type = quote_identifier(security)
            if not identifier:
                continue
            issues.append(
                DetectedIssue(
                    tenant_id=self.tenant_id,
                    provider_key="canonical",
                    connection_id=None,
                    issue_type="quote_gap",
                    affected_entity_type="security",
                    affected_entity_id=str(security.id),
                    severity="warning",
                    remediation_strategy="connector_security_enrichment",
                    context={
                        "security_id": str(security.id),
                        "identifier": identifier,
                        "identifier_type": identifier_type,
                        "max_age_hours": 48,
                        "provider_account_id": None,
                    },
                )
            )
        return await self.register_detected_issues(issues)

    async def backfill_reconciliation_findings(
        self, *, limit: int = 500
    ) -> list[DataQualityRemediationItem]:
        """Backfill only findings carrying an explicit safe strategy contract.

        Historical findings without a provider strategy remain read-only.  A
        finding is linked before the transaction ends, so rerunning this
        bounded operation is idempotent and cannot create a second work item.
        """
        from finance_sync.connectors.registry import ConnectorRegistry
        from finance_sync.models import Account
        from finance_sync.models.reconciliation import ReconciliationResult

        bounded_limit = max(1, min(limit, 500))
        findings = list(
            (
                await self.session.execute(
                    select(ReconciliationResult)
                    .where(
                        ReconciliationResult.tenant_id == self.tenant_id,
                        ReconciliationResult.remediation_item_id.is_(None),
                    )
                    .order_by(ReconciliationResult.created_at.asc())
                    .limit(bounded_limit)
                )
            )
            .scalars()
            .all()
        )
        issues: list[DetectedIssue] = []
        eligible: list[ReconciliationResult] = []
        catalog = ConnectorRegistry().list_connectors()
        account_ids = {
            str(finding.account_id)
            for finding in findings
            if isinstance(finding.account_id, str) and finding.account_id
        }
        account_rows = (
            await self.session.execute(
                select(Account).where(
                    Account.tenant_id == self.tenant_id,
                    Account.id.in_(account_ids),
                )
            )
            if account_ids
            else None
        )
        accounts = (
            {
                str(account.id): account
                for account in account_rows.scalars().all()
            }
            if account_rows is not None
            else {}
        )
        for finding in findings:
            details: dict[str, Any] = (
                finding.details if isinstance(finding.details, dict) else {}
            )
            strategy = details.get("remediation_strategy")
            context_value = details.get("remediation_context")
            context: dict[str, Any] = (
                cast("dict[str, Any]", context_value)
                if isinstance(context_value, dict)
                else {}
            )
            provider = (
                str(finding.provider_key).lower()
                if isinstance(finding.provider_key, str)
                else ""
            )
            account = (
                accounts.get(str(finding.account_id))
                if isinstance(finding.account_id, str)
                else None
            )
            declared_value = catalog.get(provider, {}).get(
                "remediation_strategies", []
            )
            declared = (
                cast("list[object]", declared_value)
                if isinstance(declared_value, list)
                else []
            )
            supports_history = any(
                isinstance(entry, dict)
                and cast("dict[str, Any]", entry).get("key")
                == "transaction_history_gap"
                for entry in declared
            )
            connection_id = context.get("connection_id")
            if (
                strategy != "transaction_history_gap"
                or not supports_history
                or not isinstance(finding.account_id, str)
                or not context.get("minimum_transactions")
                or account is None
                or account.connection_id is None
                or str(connection_id or account.connection_id)
                != str(account.connection_id)
            ):
                continue
            issue_context = {
                **context,
                "account_id": finding.account_id,
                "provider_account_id": account.external_account_id,
            }
            issues.append(
                DetectedIssue(
                    tenant_id=self.tenant_id,
                    provider_key=provider,
                    connection_id=str(account.connection_id),
                    issue_type="missing_transaction",
                    affected_entity_type="account",
                    affected_entity_id=finding.account_id,
                    severity=str(finding.severity),
                    remediation_strategy=strategy,
                    context=issue_context,
                    scope=str(finding.id),
                    detected_at=finding.created_at or datetime.now(UTC),
                )
            )
            eligible.append(finding)
        items = await self.register_detected_issues(issues)
        for finding, item in zip(eligible, items, strict=True):
            finding.remediation_item_id = str(item.id)
        if items:
            await self.session.flush()
        return items
