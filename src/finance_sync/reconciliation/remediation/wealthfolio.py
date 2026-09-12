"""Safe Wealthfolio projection strategies for canonical price remediation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from finance_sync.models.security_price import SecurityPrice
from finance_sync.reconciliation.remediation.price_history import (
    HistoricalPriceStrategy,
)
from finance_sync.reconciliation.remediation.quote import LatestQuoteStrategy
from finance_sync.reconciliation.remediation.verification import VerificationResult


class WealthfolioQuoteStrategy:
    key = "wealthfolio_quote"
    endpoint_family = "wealthfolio_quotes"
    quota_cost = 1

    def __init__(self, session: Any, settings: Any) -> None:
        self.session = session
        self.canonical = LatestQuoteStrategy(session, settings)

    def supports(self, item: Any) -> bool:
        context = _context(item)
        return bool(context.get("security_id") and context.get("remote_entity_id"))

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None:
            raise ValueError("Wealthfolio quote repair requires a target client")
        await self.canonical.execute(item)
        context = _context(item)
        row = (
            await self.session.execute(
                select(SecurityPrice)
                .where(
                    SecurityPrice.security_id == str(context["security_id"]),
                    SecurityPrice.interval == "1d",
                    SecurityPrice.price_close.is_not(None),
                )
                .order_by(SecurityPrice.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("canonical quote was not available")
        await connector.upsert_quote(
            str(context["remote_entity_id"]),
            {
                "timestamp": row.timestamp.astimezone(UTC).isoformat(),
                "close": float(row.price_close),
                "price": float(row.price_close),
                "dataSource": connector.QUOTE_DATA_SOURCE,
            },
        )

    async def verify(self, item: Any, connector: Any = None) -> VerificationResult:
        if connector is None:
            return VerificationResult(False, "Wealthfolio target client unavailable")
        context = _context(item)
        for row in await connector.get_quote_history(str(context["remote_entity_id"])):
            if (row.get("source") or row.get("dataSource")) == connector.QUOTE_DATA_SOURCE:
                return VerificationResult(True, "Wealthfolio quote is present")
        return VerificationResult(False, "Wealthfolio quote was not verified")


class WealthfolioHistoricalPriceStrategy(WealthfolioQuoteStrategy):
    key = "wealthfolio_price_history"
    endpoint_family = "wealthfolio_historical_prices"

    def __init__(self, session: Any, settings: Any) -> None:
        self.session = session
        self.canonical = HistoricalPriceStrategy(session, settings)

    def supports(self, item: Any) -> bool:
        context = _context(item)
        return bool(
            context.get("security_id")
            and context.get("remote_entity_id")
            and context.get("start_date")
            and context.get("end_date")
        )

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None:
            raise ValueError("Wealthfolio price repair requires a target client")
        await self.canonical.execute(item)
        context = _context(item)
        rows = (
            await self.session.execute(
                select(SecurityPrice).where(
                    SecurityPrice.security_id == str(context["security_id"]),
                    SecurityPrice.interval == str(context.get("interval", "1d")),
                    SecurityPrice.timestamp >= _date(context["start_date"]),
                    SecurityPrice.timestamp <= _date(context["end_date"]),
                    SecurityPrice.price_close.is_not(None),
                )
            )
        ).scalars()
        for row in rows:
            await connector.upsert_quote(
                str(context["remote_entity_id"]),
                {
                    "timestamp": row.timestamp.astimezone(UTC).isoformat(),
                    "close": float(row.price_close),
                    "price": float(row.price_close),
                    "dataSource": connector.QUOTE_DATA_SOURCE,
                },
            )


def _context(item: Any) -> dict[str, Any]:
    value = getattr(item, "context", {})
    return dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}


def _date(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
