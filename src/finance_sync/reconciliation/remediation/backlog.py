"""Persistence, deduplication and leasing for remediation work."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from finance_sync.models.remediation import DataQualityRemediationItem
from finance_sync.reconciliation.remediation.policies import scheduling_score

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ACTIVE_STATUSES = ("pending", "retry_wait", "deferred")
TERMINAL_STATUSES = ("resolved", "failed", "ignored", "manual_review")


def _fair_interleave(
    rows: list[DataQualityRemediationItem],
) -> list[DataQualityRemediationItem]:
    """Interleave locked candidates so one provider cannot fill the batch."""
    groups: dict[str, list[DataQualityRemediationItem]] = {}
    for row in rows:
        groups.setdefault(str(row.provider_key), []).append(row)
    ordered: list[DataQualityRemediationItem] = []
    providers = list(groups)
    while providers:
        next_providers: list[str] = []
        for provider in providers:
            group = groups[provider]
            ordered.append(group.pop(0))
            if group:
                next_providers.append(provider)
        providers = next_providers
    return ordered


def _stable(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(microsecond=0).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            cast("Any", value),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(value or "")


@dataclass(frozen=True, slots=True)
class DetectedIssue:
    tenant_id: str
    provider_key: str
    issue_type: str
    affected_entity_type: str
    affected_entity_id: str
    severity: str = "warning"
    priority: int = 0
    remediation_strategy: str = "unsupported"
    context: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    connection_id: str | None = None
    target_id: str | None = None
    scope: str | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def deduplication_key(issue: DetectedIssue) -> str:
    """Return a stable v1 key; run IDs and volatile context are excluded."""
    scope = issue.scope or issue.affected_entity_id
    raw = "|".join(
        (
            "dq:v1",
            f"tenant={_stable(issue.tenant_id).strip().lower()}",
            f"connection={_stable(issue.connection_id).strip().lower() or '-'}",
            f"target={_stable(issue.target_id).strip().lower() or '-'}",
            f"provider={issue.provider_key.strip().lower()}",
            f"type={issue.issue_type.strip().lower()}",
            f"entity={issue.affected_entity_type.strip().lower()}:{_stable(issue.affected_entity_id)}",
            f"scope={_stable(scope)}",
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class BacklogRepository:
    """Tenant-safe backlog operations; callers own transaction boundaries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.resolved_grace = timedelta(hours=24)

    async def register(
        self, issue: DetectedIssue
    ) -> DataQualityRemediationItem:
        from finance_sync.reconciliation.remediation.batching import (
            compatibility_key,
        )

        now = issue.detected_at.astimezone(UTC)
        key = deduplication_key(issue)
        batch_key = compatibility_key(issue)
        result = await self.session.execute(
            select(DataQualityRemediationItem).where(
                DataQualityRemediationItem.tenant_id == issue.tenant_id,
                DataQualityRemediationItem.deduplication_key == key,
            )
        )
        existing = result.scalar_one_or_none()
        if inspect.isawaitable(existing):
            existing = await existing
        if existing is not None and existing.status == "resolved":
            if (
                existing.resolved_at
                and now - existing.resolved_at <= self.resolved_grace
            ):
                existing.last_seen_at = now
                existing.context = issue.context
                await self.session.flush()
                return existing
            # A finding that returns after the grace period is a new
            # generation, while retaining the resolved item as history.
            generation = now.replace(microsecond=0).isoformat()
            key = hashlib.sha256(
                f"{key}|generation={generation}".encode()
            ).hexdigest()
        values = {
            "id": uuid4(),
            "tenant_id": issue.tenant_id,
            "provider_key": issue.provider_key.strip().lower(),
            "connection_id": issue.connection_id,
            "target_id": issue.target_id,
            "issue_type": issue.issue_type,
            "affected_entity_type": issue.affected_entity_type,
            "affected_entity_id": issue.affected_entity_id,
            "severity": issue.severity,
            "priority": issue.priority,
            "status": "pending",
            "first_detected_at": now,
            "last_seen_at": now,
            "next_attempt_at": now,
            "remediation_strategy": issue.remediation_strategy,
            "deduplication_key": key,
            "batch_key": batch_key[:256],
            "context": {
                **issue.context,
                "generation": key != deduplication_key(issue),
            },
        }
        stmt = insert(DataQualityRemediationItem).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_dq_remediation_tenant_dedup",
            set_={
                "last_seen_at": now,
                "severity": stmt.excluded.severity,
                "priority": stmt.excluded.priority,
                "remediation_strategy": stmt.excluded.remediation_strategy,
                "batch_key": stmt.excluded.batch_key,
                "context": stmt.excluded.context,
            },
        ).returning(DataQualityRemediationItem)
        result = await self.session.execute(stmt)
        created = result.scalar_one()
        if inspect.isawaitable(created):
            created = await created
        return created

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        provider_key: str | None = None,
        issue_type: str | None = None,
        severity: str | None = None,
        connection_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DataQualityRemediationItem]:
        conditions: list[Any] = [
            DataQualityRemediationItem.tenant_id == tenant_id
        ]
        for column, value in (
            (DataQualityRemediationItem.status, status),
            (DataQualityRemediationItem.provider_key, provider_key),
            (DataQualityRemediationItem.issue_type, issue_type),
            (DataQualityRemediationItem.severity, severity),
            (DataQualityRemediationItem.connection_id, connection_id),
        ):
            if value is not None:
                conditions.append(column == value)
        result = await self.session.execute(
            select(DataQualityRemediationItem)
            .where(*conditions)
            .order_by(
                DataQualityRemediationItem.priority.desc(),
                DataQualityRemediationItem.first_detected_at,
            )
            .offset(offset)
            .limit(min(limit, 200))
        )
        return list(result.scalars().all())

    async def get(
        self, tenant_id: str, item_id: str
    ) -> DataQualityRemediationItem | None:
        return (
            await self.session.execute(
                select(DataQualityRemediationItem).where(
                    DataQualityRemediationItem.tenant_id == tenant_id,
                    DataQualityRemediationItem.id == item_id,
                )
            )
        ).scalar_one_or_none()

    async def claim(
        self,
        *,
        limit: int = 20,
        lease_seconds: int = 300,
        tenant_id: str | None = None,
    ) -> list[DataQualityRemediationItem]:
        now = datetime.now(UTC)
        bounded_limit = max(1, min(limit, 200))
        candidate_limit = min(max(bounded_limit * 4, bounded_limit), 200)
        due = or_(
            DataQualityRemediationItem.status.in_(ACTIVE_STATUSES),
            and_(
                DataQualityRemediationItem.status == "processing",
                DataQualityRemediationItem.lease_expires_at < now,
            ),
        )
        conditions = [due, DataQualityRemediationItem.next_attempt_at <= now]
        if tenant_id is not None:
            conditions.append(DataQualityRemediationItem.tenant_id == tenant_id)
        rows = list(
            (
                await self.session.execute(
                    select(DataQualityRemediationItem)
                    .where(*conditions)
                    .order_by(
                        DataQualityRemediationItem.priority.desc(),
                        DataQualityRemediationItem.first_detected_at,
                    )
                    .limit(candidate_limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        rows.sort(
            key=lambda row: (
                -scheduling_score(row, now=now),
                row.first_detected_at,
                str(row.id),
            )
        )
        rows = _fair_interleave(rows)[:bounded_limit]
        for row in rows:
            row.status = "processing"
            row.claim_token = uuid4()
            row.claimed_at = now
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.attempt_count += 1
        await self.session.flush()
        return rows

    async def transition(
        self,
        tenant_id: str,
        item_id: str,
        *,
        status: str,
        claim_token: str | UUID | None = None,
        allowed_from: tuple[str, ...] | None = None,
        error: str | None = None,
        error_category: str | None = None,
        next_attempt_at: datetime | None = None,
        increment_rate_limit_deferrals: bool = False,
    ) -> bool:
        if status not in {
            "pending",
            "processing",
            "deferred",
            "retry_wait",
            "resolved",
            "failed",
            "ignored",
            "manual_review",
        }:
            message = f"unsupported remediation status: {status}"
            raise ValueError(message)
        conditions = [
            DataQualityRemediationItem.id == item_id,
            DataQualityRemediationItem.tenant_id == tenant_id,
        ]
        if allowed_from is not None:
            conditions.append(
                DataQualityRemediationItem.status.in_(allowed_from)
            )
        if claim_token is not None:
            conditions.append(
                DataQualityRemediationItem.claim_token == claim_token
            )
        values: dict[str, Any] = {
            "status": status,
            "last_error": error,
            "last_error_category": error_category,
            "next_attempt_at": next_attempt_at or datetime.now(UTC),
            "lease_expires_at": None,
            "claim_token": None,
        }
        if increment_rate_limit_deferrals:
            values["rate_limit_deferral_count"] = (
                DataQualityRemediationItem.rate_limit_deferral_count + 1
            )
        if status == "resolved":
            verified_at = datetime.now(UTC)
            values["resolved_at"] = verified_at
            values["verified_at"] = verified_at
        result = await self.session.execute(
            update(DataQualityRemediationItem)
            .where(*conditions)
            .values(**values)
        )
        return bool(cast("Any", result).rowcount)

    async def cleanup_terminal(
        self, *, retention_days: int, tenant_id: str | None = None
    ) -> int:
        """Delete only old resolved/ignored rows; never active/manual work."""
        cutoff = datetime.now(UTC) - timedelta(days=max(1, retention_days))
        conditions: list[Any] = [
            DataQualityRemediationItem.status.in_(("resolved", "ignored")),
            DataQualityRemediationItem.updated_at < cutoff,
        ]
        if tenant_id is not None:
            conditions.append(DataQualityRemediationItem.tenant_id == tenant_id)
        result = await self.session.execute(
            delete(DataQualityRemediationItem).where(*conditions)
        )
        return int(cast("Any", result).rowcount or 0)

    async def cleanup_operator_audit(self, *, retention_days: int) -> int:
        """Expire only audit rows created by remediation operator actions."""
        from finance_sync.models.connection_audit_log import ConnectionAuditLog

        cutoff = datetime.now(UTC) - timedelta(days=max(1, retention_days))
        result = await self.session.execute(
            delete(ConnectionAuditLog).where(
                ConnectionAuditLog.provider_key == "remediation",
                ConnectionAuditLog.created_at < cutoff,
            )
        )
        return int(cast("Any", result).rowcount or 0)

    async def mark_connection_auth_failure(
        self,
        tenant_id: str,
        connection_id: str | UUID | None,
        *,
        error: str,
    ) -> bool:
        """Publish a safe reauthentication signal for one tenant connection."""
        if connection_id is None:
            return False
        from finance_sync.models.credential import Credential

        now = datetime.now(UTC)
        result = await self.session.execute(
            update(Credential)
            .where(
                Credential.tenant_id == tenant_id,
                Credential.id == str(connection_id),
            )
            .values(
                credential_status="reauth_required",
                reauth_required_at=now,
                last_error_category="authentication",
                last_error=error[:512],
            )
        )
        return bool(cast("Any", result).rowcount)
