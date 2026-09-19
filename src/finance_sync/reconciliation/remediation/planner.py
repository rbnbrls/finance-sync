"""Planner facade: claims due work and keeps provider calls out of planning."""

from __future__ import annotations

from typing import TYPE_CHECKING

from finance_sync.reconciliation.remediation.backlog import BacklogRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class RemediationPlanner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        claim_limit: int = 20,
        lease_seconds: int = 300,
    ) -> None:
        self.backlog = BacklogRepository(session)
        self.claim_limit = claim_limit
        self.lease_seconds = lease_seconds

    async def claim_due(self, *, tenant_id: str | None = None):
        return await self.backlog.claim(
            limit=self.claim_limit,
            lease_seconds=self.lease_seconds,
            tenant_id=tenant_id,
        )
