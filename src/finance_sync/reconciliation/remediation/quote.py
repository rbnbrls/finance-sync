"""Provider-neutral latest-quote remediation strategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import exists, func, select

from finance_sync.enrichment.gateway import EnrichmentGateway
from finance_sync.enrichment.price_store import PriceStore
from finance_sync.models.holding import Holding
from finance_sync.models.security_price import SecurityPrice
from finance_sync.reconciliation.remediation.verification import (
    VerificationResult,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class LatestQuoteStrategy:
    """Fetch and verify a recent local quote for one held security."""

    key = "latest_quote_enrichment"
    endpoint_family = "quotes"
    quota_cost = 1

    def supports(self, item: Any) -> bool:
        """Only execute findings with an explicit held-security contract."""
        context = _context(item)
        return bool(context.get("security_id") and context.get("identifier"))

    def __init__(self, session: AsyncSession, settings: Any) -> None:
        from finance_sync.db.uow import UnitOfWork

        self.session = session
        self.gateway = EnrichmentGateway(
            settings=settings,
            uow=UnitOfWork(session),
            price_store=PriceStore(session, settings),
        )

    async def execute(self, item: Any, connector: Any = None) -> None:
        del connector
        context = _context(item)
        await self.gateway.get_latest_quote(
            security_id=str(context["security_id"]),
            identifier=str(context["identifier"]),
            identifier_type=str(context.get("identifier_type", "ticker")),
        )

    async def execute_batch(
        self, items: list[Any], connector: Any = None
    ) -> dict[str, str]:
        """Fetch one latest quote for compatible items sharing a security."""
        del connector
        if not items:
            return {}
        first = _context(items[0])
        await self.gateway.get_latest_quote(
            security_id=str(first["security_id"]),
            identifier=str(first["identifier"]),
            identifier_type=str(first.get("identifier_type", "ticker")),
        )
        return {str(item.id): "success" for item in items}

    async def verify(self, item: Any) -> VerificationResult:
        context = _context(item)
        security_id = str(context.get("security_id", ""))
        if not security_id:
            return VerificationResult(False, "quote gap has no security scope")
        max_age_hours = max(1, min(int(context.get("max_age_hours", 48)), 168))
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        filters = [
            SecurityPrice.security_id == security_id,
            SecurityPrice.interval == "1d",
            SecurityPrice.timestamp >= cutoff,
            SecurityPrice.price_close.is_not(None),
        ]
        tenant_id = getattr(item, "tenant_id", None)
        if tenant_id is not None:
            filters.append(
                exists(
                    select(Holding.id).where(
                        Holding.tenant_id == tenant_id,
                        Holding.security_id == security_id,
                    )
                )
            )
        count = await self.session.scalar(
            select(func.count()).select_from(SecurityPrice).where(*filters)
        )
        resolved = int(count or 0) >= 1
        return VerificationResult(
            resolved,
            "recent quote observation exists"
            if resolved
            else "no recent quote observation exists",
        )


def _context(item: Any) -> dict[str, Any]:
    value = getattr(item, "context", {})
    return (
        dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}
    )
