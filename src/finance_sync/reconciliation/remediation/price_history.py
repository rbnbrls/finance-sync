"""Provider-neutral historical price remediation strategy."""

from __future__ import annotations

from datetime import UTC, datetime
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


class HistoricalPriceStrategy:
    """Fill an explicitly scoped price window through EnrichmentGateway."""

    key = "historical_price_enrichment"
    endpoint_family = "historical_prices"
    quota_cost = 1

    def supports(self, item: Any) -> bool:
        """Require the complete, explicit price-window contract."""
        context = _context(item)
        return bool(
            all(
                context.get(key)
                for key in (
                    "security_id",
                    "identifier",
                    "start_date",
                    "end_date",
                )
            )
            and str(context.get("interval", "1d"))
            in {"1d", "1h", "5m", "1m"}
        )

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
        await self.gateway.get_historical_prices(
            security_id=str(context["security_id"]),
            identifier=str(context["identifier"]),
            identifier_type=str(context.get("identifier_type", "ticker")),
            interval=str(context.get("interval", "1d")),
            start_date=_date(context.get("start_date")),
            end_date=_date(context.get("end_date")),
            limit=min(int(context.get("limit", 365)), 1000),
        )

    async def execute_batch(
        self, items: list[Any], connector: Any = None
    ) -> dict[str, str]:
        """Coalesce one security's compatible historical windows."""
        del connector
        if not items:
            return {}
        contexts = [_context(item) for item in items]
        windows = [
            (_date(context.get("start_date")), _date(context.get("end_date")))
            for context in contexts
        ]
        if any(
            start is None or end is None or start >= end
            for start, end in windows
        ):
            return await self._execute_batch_individually(items)
        starts = [start for start, _ in windows if start is not None]
        ends = [end for _, end in windows if end is not None]
        earliest = min(starts)
        latest = max(ends)
        if (latest - earliest).days + 1 > 1000:
            return await self._execute_batch_individually(items)
        first = contexts[0]
        await self.gateway.get_historical_prices(
            security_id=str(first["security_id"]),
            identifier=str(first["identifier"]),
            identifier_type=str(first.get("identifier_type", "ticker")),
            interval=str(first.get("interval", "1d")),
            start_date=earliest,
            end_date=latest,
            limit=min(max(int(first.get("limit", 365)), 1), 1000),
        )
        return {str(item.id): "success" for item in items}

    async def _execute_batch_individually(
        self, items: list[Any]
    ) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for item in items:
            try:
                await self.execute(item)
            except Exception:
                outcomes[str(item.id)] = "retry"
            else:
                outcomes[str(item.id)] = "success"
        return outcomes

    async def verify(self, item: Any) -> VerificationResult:
        context = _context(item)
        security_id = str(context.get("security_id", ""))
        interval = str(context.get("interval", "1d"))
        start = _date(context.get("start_date"))
        end = _date(context.get("end_date")) or datetime.now(UTC)
        if not security_id or start is None or start >= end:
            return VerificationResult(False, "price gap has no valid scope")
        filters = [
                SecurityPrice.security_id == security_id,
                SecurityPrice.interval == interval,
                SecurityPrice.timestamp >= start,
                SecurityPrice.timestamp < end,
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
        query = select(func.count()).select_from(SecurityPrice).where(*filters)
        count = int(await self.session.scalar(query) or 0)
        expected = int(context.get("minimum_observations", 1))
        return VerificationResult(
            count >= expected,
            f"found {count} price observations; expected {expected}",
        )


def _date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return (
            parsed.astimezone(UTC)
            if parsed.tzinfo
            else parsed.replace(tzinfo=UTC)
        )
    return None


def _context(item: Any) -> dict[str, Any]:
    value = getattr(item, "context", {})
    return (
        dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}
    )
