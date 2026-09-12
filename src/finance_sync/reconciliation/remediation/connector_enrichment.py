"""Enrichment through an authenticated broker connector.

Connectors are the authoritative source for the holdings they import.  This
strategy lets API connectors repair missing current prices without coupling
the remediation executor to a particular broker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import exists, func, select

from finance_sync.enrichment.models import PriceObservation
from finance_sync.enrichment.price_store import PriceStore
from finance_sync.models.holding import Holding
from finance_sync.models.security import Security
from finance_sync.models.security_price import SecurityPrice
from finance_sync.reconciliation.remediation.verification import (
    VerificationResult,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.connectors.models import SecurityReference


class ConnectorSecurityEnrichmentStrategy:
    """Use any authenticated API connector that exposes holdings."""

    key = "connector_security_enrichment"
    endpoint_family = "connector_holdings"
    quota_cost = 1

    def __init__(self, session: AsyncSession, settings: Any) -> None:
        self.session = session
        self.prices = PriceStore(session, settings)

    def supports(self, item: Any) -> bool:
        context = _context(item)
        return bool(
            item.connection_id
            and context.get("security_id")
            and context.get("identifier")
        )

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None or not callable(
            getattr(connector, "fetch_holdings", None)
        ):
            message = "connector does not expose holdings"
            raise RuntimeError(message)
        context = _context(item)
        holdings = await connector.fetch_holdings(
            account_id=context.get("provider_account_id")
        )
        security_id = str(context["security_id"])
        security = await self.session.get(Security, security_id)
        if security is None:
            message = "security not found"
            raise RuntimeError(message)
        target = _find_holding(
            holdings, security, str(context["identifier"])
        )
        if target is None or target.price is None:
            message = "connector returned no current price"
            raise RuntimeError(message)
        observed_at = target.observed_at.astimezone(UTC)
        await self.prices.store_prices(
            [
                PriceObservation(
                    security_id=security_id,
                    timestamp=observed_at,
                    price_close=target.price,
                    source=str(item.provider_key),
                    interval="1d",
                    currency_code=target.price_currency or target.currency_code,
                    venue=target.security_reference.venue,
                )
            ]
        )

    async def execute_batch(
        self, items: list[Any], connector: Any = None
    ) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for item in items:
            try:
                await self.execute(item, connector)
            except Exception:
                outcomes[str(item.id)] = "retry"
            else:
                outcomes[str(item.id)] = "success"
        return outcomes

    async def verify(self, item: Any) -> VerificationResult:
        context = _context(item)
        security_id = str(context.get("security_id", ""))
        if not security_id:
            return VerificationResult(False, "enrichment has no security scope")
        cutoff = datetime.now(UTC) - timedelta(hours=48)
        count = await self.session.scalar(
            select(func.count())
            .select_from(SecurityPrice)
            .where(
                SecurityPrice.security_id == security_id,
                SecurityPrice.timestamp >= cutoff,
                SecurityPrice.price_close.is_not(None),
                exists(
                    select(Holding.id).where(
                        Holding.tenant_id == item.tenant_id,
                        Holding.security_id == security_id,
                    )
                ),
            )
        )
        resolved = int(count or 0) > 0
        return VerificationResult(
            resolved,
            "recent connector price exists"
            if resolved
            else "no recent connector price",
        )


def _context(item: Any) -> dict[str, Any]:
    value = getattr(item, "context", {})
    return (
        dict(cast("dict[str, Any]", value))
        if isinstance(value, dict)
        else {}
    )


def _find_holding(
    holdings: list[Any], security: Security, identifier: str
) -> Any | None:
    wanted = {
        _normalise_symbol(value)
        for value in (identifier, security.ticker or "", security.isin or "")
        if value
    }
    for holding in holdings:
        reference: SecurityReference = holding.security_reference
        values = {
            _normalise_symbol(value)
            for value in (
                reference.external_id,
                reference.ticker,
                reference.isin,
            )
            if value
        }
        if wanted & values:
            return holding
    return None


def _normalise_symbol(value: str) -> str:
    """Compare venue-qualified and provider-local ticker forms safely."""
    normalised = str(value).strip().upper()
    if ":" in normalised:
        normalised = normalised.split(":", 1)[0]
    if normalised.endswith("_EQ"):
        normalised = normalised[:-3]
    return normalised
