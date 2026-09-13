"""Safe Wealthfolio projection strategies for canonical price remediation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from finance_sync.models.security_price import SecurityPrice
from finance_sync.reconciliation.remediation.price_history import (
    HistoricalPriceStrategy,
    normalize_window,
)
from finance_sync.reconciliation.remediation.quote import LatestQuoteStrategy
from finance_sync.reconciliation.remediation.verification import (
    VerificationResult,
)
from finance_sync.services.fx_service import FxService


class WealthfolioQuoteStrategy:
    key = "wealthfolio_quote"
    endpoint_family = "wealthfolio_quotes"
    quota_cost = 1

    def __init__(self, session: Any, settings: Any) -> None:
        self.session = session
        self.canonical = LatestQuoteStrategy(session, settings)

    def supports(self, item: Any) -> bool:
        context = _context(item)
        return bool(
            context.get("security_id") and context.get("remote_entity_id")
        )

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None:
            msg = "Wealthfolio quote repair requires a target client"
            raise ValueError(msg)
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
            msg = "canonical quote was not available"
            raise ValueError(msg)
        await connector.upsert_quote(
            str(context["remote_entity_id"]),
            {
                "id": (
                    f"{context['remote_entity_id']}_"
                    f"{row.timestamp.timestamp()}_FINANCE_SYNC"
                ),
                "createdAt": datetime.now(UTC).isoformat(),
                "source": connector.QUOTE_DATA_SOURCE,
                "assetId": str(context["remote_entity_id"]),
                "timestamp": row.timestamp.astimezone(UTC).isoformat(),
                "open": float(row.price_close),
                "high": float(row.price_close),
                "low": float(row.price_close),
                "volume": 0,
                "close": float(row.price_close),
                "adjclose": float(row.price_close),
                "currency": str(context.get("currency") or "EUR"),
                "dataSource": connector.QUOTE_DATA_SOURCE,
            },
        )

    async def verify(
        self, item: Any, connector: Any = None
    ) -> VerificationResult:
        if connector is None:
            return VerificationResult(
                False, "Wealthfolio target client unavailable"
            )
        context = _context(item)
        for row in await connector.get_quote_history(
            str(context["remote_entity_id"])
        ):
            if (
                row.get("source") or row.get("dataSource")
            ) == connector.QUOTE_DATA_SOURCE:
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
        start, end = normalize_window(
            context.get("start_date"), context.get("end_date")
        )
        return bool(
            context.get("security_id")
            and context.get("remote_entity_id")
            and context.get("start_date")
            and context.get("end_date")
            and start is not None
            and end is not None
            and start < end
        )

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None:
            msg = "Wealthfolio price repair requires a target client"
            raise ValueError(msg)
        context = _context(item)
        start, end = normalize_window(
            context.get("start_date"), context.get("end_date")
        )
        if start is None or end is None or start >= end:
            msg = "price gap has no valid half-open window"
            raise ValueError(msg)
        await self.canonical.execute(item)
        rows = (
            await self.session.execute(
                select(SecurityPrice).where(
                    SecurityPrice.security_id == str(context["security_id"]),
                    SecurityPrice.interval
                    == str(context.get("interval", "1d")),
                    SecurityPrice.timestamp >= start,
                    SecurityPrice.timestamp < end,
                    SecurityPrice.price_close.is_not(None),
                )
            )
        ).scalars()
        for row in rows:
            await connector.upsert_quote(
                str(context["remote_entity_id"]),
                {
                    "id": (
                        f"{context['remote_entity_id']}_"
                        f"{row.timestamp.timestamp()}_FINANCE_SYNC"
                    ),
                    "createdAt": datetime.now(UTC).isoformat(),
                    "source": connector.QUOTE_DATA_SOURCE,
                    "assetId": str(context["remote_entity_id"]),
                    "timestamp": row.timestamp.astimezone(UTC).isoformat(),
                    "open": float(row.price_close),
                    "high": float(row.price_close),
                    "low": float(row.price_close),
                    "volume": 0,
                    "close": float(row.price_close),
                    "adjclose": float(row.price_close),
                    "currency": str(context.get("currency") or "EUR"),
                    "dataSource": connector.QUOTE_DATA_SOURCE,
                },
            )

    async def verify(
        self, item: Any, connector: Any = None
    ) -> VerificationResult:
        """Verify owned remote observations in the same half-open window."""
        if connector is None:
            return VerificationResult(
                False, "Wealthfolio target client unavailable"
            )
        context = _context(item)
        start, end = normalize_window(
            context.get("start_date"), context.get("end_date")
        )
        if start is None or end is None or start >= end:
            return VerificationResult(
                False, "price gap has no valid half-open window"
            )
        count = 0
        for row in await connector.get_quote_history(
            str(context["remote_entity_id"])
        ):
            if (
                row.get("source") or row.get("dataSource")
            ) != connector.QUOTE_DATA_SOURCE:
                continue
            try:
                timestamp = datetime.fromisoformat(str(row.get("timestamp")))
                timestamp = (
                    timestamp.astimezone(UTC)
                    if timestamp.tzinfo
                    else timestamp.replace(tzinfo=UTC)
                )
            except (TypeError, ValueError):
                continue
            if start <= timestamp < end:
                count += 1
        expected = max(1, int(context.get("minimum_observations", 1)))
        return VerificationResult(
            count >= expected,
            f"found {count} Wealthfolio price observations; "
            f"expected {expected}",
        )


class WealthfolioFxStrategy:
    """Refresh a Wealthfolio FX pair from finance-sync's canonical service."""

    key = "wealthfolio_fx"
    endpoint_family = "wealthfolio_fx"
    quota_cost = 1

    def __init__(self, session: Any, settings: Any) -> None:
        from finance_sync.db.uow import UnitOfWork

        self.session = session
        self.fx = FxService(settings, UnitOfWork(session))

    def supports(self, item: Any) -> bool:
        context = _context(item)
        return bool(context.get("from_currency") and context.get("to_currency"))

    async def execute(self, item: Any, connector: Any = None) -> None:
        if connector is None:
            message = "Wealthfolio FX repair requires a target client"
            raise ValueError(message)
        context = _context(item)
        observation = await self.fx.get_rate(
            str(context["from_currency"]), str(context["to_currency"])
        )
        if observation is None:
            message = "canonical FX rate was not available"
            raise ValueError(message)
        await connector.add_exchange_rate(
            from_currency=observation.base_currency,
            to_currency=observation.quote_currency,
            rate=str(observation.rate),
            source="FINANCE_SYNC",
        )

    async def verify(
        self, _item: Any, connector: Any = None
    ) -> VerificationResult:
        return VerificationResult(
            connector is not None,
            "Wealthfolio FX rate was submitted"
            if connector is not None
            else "Wealthfolio target client unavailable",
        )


def _context(item: Any) -> dict[str, Any]:
    value = getattr(item, "context", {})
    return (
        dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}
    )
