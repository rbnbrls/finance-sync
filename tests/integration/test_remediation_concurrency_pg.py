"""Real PostgreSQL/Redis concurrency coverage for remediation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from finance_sync.models import Tenant
from finance_sync.reconciliation.remediation.backlog import (
    BacklogRepository,
    DetectedIssue,
)
from finance_sync.reconciliation.remediation.rate_limit import (
    QuotaPolicy,
    RemediationRateLimitCoordinator,
)

pytestmark = pytest.mark.integration


async def _tenant(session_factory) -> str:
    tenant_id = str(uuid4())
    async with session_factory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"remediation-{tenant_id[:8]}",
                name="Remediation",
            )
        )
        await session.commit()
    return tenant_id


def _issue(tenant_id: str, *, entity: str = "account-1") -> DetectedIssue:
    return DetectedIssue(
        tenant_id=tenant_id,
        provider_key="trading212",
        issue_type="unresolved_security",
        affected_entity_type="unresolved_security",
        affected_entity_id=entity,
        remediation_strategy="trading212_instrument_metadata",
        detected_at=datetime.now(UTC),
    )


async def test_concurrent_registers_share_one_deduplicated_row(
    session_factory,
) -> None:
    tenant_id = await _tenant(session_factory)
    issue = _issue(tenant_id)

    async def register_once() -> str:
        async with session_factory() as session:
            item = await BacklogRepository(session).register(issue)
            await session.commit()
            return str(item.id)

    ids = await asyncio.gather(*(register_once() for _ in range(8)))
    assert len(set(ids)) == 1


async def test_concurrent_claims_split_due_work_between_workers(
    session_factory,
) -> None:
    tenant_id = await _tenant(session_factory)
    async with session_factory() as session:
        repository = BacklogRepository(session)
        for index in range(4):
            await repository.register(
                _issue(tenant_id, entity=f"account-{index}")
            )
        await session.commit()

    async def claim_once() -> set[str]:
        async with session_factory() as session:
            rows = await BacklogRepository(session).claim(
                tenant_id=tenant_id, limit=4, lease_seconds=60
            )
            await session.commit()
            return {str(row.id) for row in rows}

    first, second = await asyncio.gather(claim_once(), claim_once())
    assert first.isdisjoint(second)
    assert len(first | second) == 4


async def test_redis_quota_allows_only_shared_window_budget(
    redis_client,
) -> None:
    coordinator = RemediationRateLimitCoordinator(redis_client)
    policy = QuotaPolicy(5, 60, "metadata")

    reservations = await asyncio.gather(
        *(
            coordinator.reserve("trading212", "connection-1", policy)
            for _ in range(20)
        )
    )
    assert sum(reservation.allowed for reservation in reservations) == 5
