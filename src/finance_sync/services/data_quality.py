"""Tenant-scoped projection of reconciliation and canonical data quality."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from finance_sync.models import (
    ReconciliationResult,
    ReconciliationRun,
    Transaction,
)
from finance_sync.schemas.control_plane import ControlPlaneAction
from finance_sync.schemas.data_quality import (
    DataQualityCoverage,
    DataQualityIssue,
    DataQualityOverview,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class DataQualityService:
    """Build a read-only, tenant-isolated data-quality view."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now or datetime.now(UTC)

    async def get_overview(self) -> DataQualityOverview:
        run = await self._latest_run()
        coverage = await self._coverage()
        if run is None:
            return DataQualityOverview(
                status="unavailable",
                coverage=coverage,
                generated_at=self._now,
            )

        results = list(
            (
                await self._session.execute(
                    select(ReconciliationResult)
                    .where(
                        ReconciliationResult.tenant_id == self._tenant_id,
                        ReconciliationResult.run_id == run.id,
                    )
                    .order_by(ReconciliationResult.created_at.desc())
                )
            ).scalars()
        )
        issues = [self._issue(result) for result in results]
        by_kind: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for result in results:
            by_kind[str(result.kind)] = by_kind.get(str(result.kind), 0) + 1
            by_severity[str(result.severity)] = (
                by_severity.get(str(result.severity), 0) + 1
            )
        return DataQualityOverview(
            status=(
                "attention_required"
                if run.status != "completed" or issues
                else "healthy"
            ),
            latest_run_id=str(run.id),
            latest_run_status=str(run.status),
            latest_run_at=run.completed_at or run.started_at,
            findings_total=len(results),
            findings_by_kind=by_kind,
            findings_by_severity=by_severity,
            coverage=coverage,
            issues=issues,
            generated_at=self._now,
        )

    async def _latest_run(self) -> ReconciliationRun | None:
        return (
            await self._session.execute(
                select(ReconciliationRun)
                .where(ReconciliationRun.tenant_id == self._tenant_id)
                .order_by(ReconciliationRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _coverage(self) -> list[DataQualityCoverage]:
        rows = (
            await self._session.execute(
                select(
                    Transaction.provider_key,
                    func.count(func.distinct(Transaction.account_id)),
                    func.count(Transaction.id),
                    func.min(Transaction.occurred_at),
                    func.max(Transaction.occurred_at),
                )
                .where(Transaction.tenant_id == self._tenant_id)
                .group_by(Transaction.provider_key)
                .order_by(Transaction.provider_key)
            )
        ).all()
        return [
            DataQualityCoverage(
                provider=str(row[0]),
                accounts=int(row[1] or 0),
                transactions=int(row[2] or 0),
                first_transaction_at=row[3],
                last_transaction_at=row[4],
            )
            for row in rows
        ]

    @staticmethod
    def _issue(result: ReconciliationResult) -> DataQualityIssue:
        kind = str(result.kind)
        labels = {
            "duplicate_transaction": "Mogelijke dubbele transactie",
            "missing_transaction": "Ontbrekende transactie",
            "cross_connector_mismatch": "Brondekking wijkt af",
            "amount_mismatch": "Bedragen komen niet overeen",
        }
        transaction_ids = [
            str(value)
            for value in (result.transaction_id_a, result.transaction_id_b)
            if value
        ]
        external_ids = [
            str(value)
            for value in (
                result.external_transaction_id_a,
                result.external_transaction_id_b,
            )
            if value
        ]
        return DataQualityIssue(
            id=f"data-quality:{result.id}",
            kind=kind,  # type: ignore[arg-type]
            severity=str(result.severity),  # type: ignore[arg-type]
            title=labels.get(kind, "Datakwaliteitsprobleem"),
            description=(
                result.description or "Controleer de betrokken bronrecords."
            ),
            provider=result.provider_key,
            other_provider=result.other_provider_key,
            account_id=str(result.account_id) if result.account_id else None,
            transaction_ids=transaction_ids,
            external_record_ids=external_ids,
            impact_count=max(len(transaction_ids), 1),
            action=ControlPlaneAction(
                key="view_reconciliation",
                label="Finding bekijken",
                method="GET",
                path=f"/api/v1/reconciliation/{result.run_id}",
                permission="reconciliation:read",
            ),
        )
