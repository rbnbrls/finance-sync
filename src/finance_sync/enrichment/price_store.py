"""PriceStore service — stores, deduplicates, and prunes
time-series price data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, func, select

from finance_sync.enrichment.models import PriceObservation
from finance_sync.models.security_price import SecurityPrice

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.config.settings import Settings


class PriceStore:
    """Stores and manages price observations.

    Handles deduplication by (security_id, timestamp, source),
    bulk insertion, and pruning of old intraday data.
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ── Store ────────────────────────────────────────────────────────────

    async def store_prices(self, observations: list[PriceObservation]) -> int:
        """Insert price observations, deduplicating by
        (security_id, timestamp, source).

        Returns the number of new rows inserted.
        """
        if not observations:
            return 0

        inserted = 0
        for obs in observations:
            existing = await self._find_existing(
                security_id=obs.security_id,
                timestamp=obs.timestamp,
                source=obs.source,
                interval=obs.interval,
            )
            if existing is not None:
                continue

            price = SecurityPrice(
                security_id=obs.security_id,
                timestamp=obs.timestamp,
                price_open=obs.price_open,
                price_high=obs.price_high,
                price_low=obs.price_low,
                price_close=obs.price_close,
                volume=obs.volume,
                source=obs.source,
                interval=obs.interval,
                currency_code=obs.currency_code,
            )
            self._session.add(price)
            inserted += 1

        if inserted:
            await self._session.flush()

        return inserted

    async def _find_existing(
        self,
        security_id: str,
        timestamp: datetime,
        source: str,
        interval: str,
    ) -> SecurityPrice | None:
        """Check if a price observation already exists."""
        stmt = select(SecurityPrice).where(
            SecurityPrice.security_id == security_id,  # type: ignore[attr-defined]
            SecurityPrice.timestamp == timestamp,  # type: ignore[attr-defined]
            SecurityPrice.source == source,  # type: ignore[attr-defined]
            SecurityPrice.interval == interval,  # type: ignore[attr-defined]
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Query ────────────────────────────────────────────────────────────

    async def get_latest_price(
        self,
        security_id: str,
        interval: str = "1d",
    ) -> PriceObservation | None:
        """Return the most recent price observation for a security."""
        stmt = (
            select(SecurityPrice)
            .where(
                SecurityPrice.security_id == security_id,  # type: ignore[attr-defined]
                SecurityPrice.interval == interval,  # type: ignore[attr-defined]
            )
            .order_by(SecurityPrice.timestamp.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return self._to_observation(row) if row else None

    async def get_price_history(
        self,
        security_id: str,
        interval: str = "1d",
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[PriceObservation]:
        """Return price history for a security."""
        conditions = [
            SecurityPrice.security_id == security_id,  # type: ignore[attr-defined]
            SecurityPrice.interval == interval,  # type: ignore[attr-defined]
        ]
        if start is not None:
            conditions.append(
                SecurityPrice.timestamp >= start  # type: ignore[attr-defined]
            )
        if end is not None:
            conditions.append(
                SecurityPrice.timestamp <= end  # type: ignore[attr-defined]
            )

        stmt = (
            select(SecurityPrice)
            .where(and_(*conditions))
            .order_by(SecurityPrice.timestamp.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [self._to_observation(row) for row in result.scalars().all()]

    async def has_prices(self, security_id: str, interval: str = "1d") -> bool:
        """Check if a security has any price data."""
        stmt = (
            select(func.count())
            .select_from(SecurityPrice)
            .where(
                SecurityPrice.security_id == security_id,  # type: ignore[attr-defined]
                SecurityPrice.interval == interval,  # type: ignore[attr-defined]
            )
        )
        result = await self._session.execute(stmt)
        count: int = result.scalar() or 0  # type: ignore[assignment]
        return count > 0

    # ── Pruning ──────────────────────────────────────────────────────────

    async def prune_intraday_data(self) -> int:
        """Delete intraday price data older than the configured retention.

        Returns the number of rows removed.
        """
        kept_since = datetime.now(UTC) - timedelta(
            days=self._settings.price_store_keep_minute_days
        )

        stmt = delete(SecurityPrice).where(
            SecurityPrice.interval.in_(["1m", "5m", "15m", "30m"]),  # type: ignore[attr-defined]
            SecurityPrice.timestamp < kept_since,  # type: ignore[attr-defined]
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount  # type: ignore[return-value]

    async def prune_hourly_data(self) -> int:
        """Delete hourly price data older than the configured retention.

        Returns the number of rows removed.
        """
        kept_since = datetime.now(UTC) - timedelta(
            days=self._settings.price_store_keep_hour_days
        )

        stmt = delete(SecurityPrice).where(
            SecurityPrice.interval.in_(["1h", "4h"]),  # type: ignore[attr-defined]
            SecurityPrice.timestamp < kept_since,  # type: ignore[attr-defined]
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount  # type: ignore[return-value]

    # ── Aggregates ───────────────────────────────────────────────────────

    async def count_total_prices(self) -> int:
        """Return total number of price observations."""
        result = await self._session.execute(
            select(func.count()).select_from(SecurityPrice)
        )
        return result.scalar() or 0  # type: ignore[return-value]

    async def count_securities_with_prices(self) -> int:
        """Return number of distinct securities that have price data."""
        stmt = select(func.count(func.distinct(SecurityPrice.security_id)))  # type: ignore[attr-defined]
        result = await self._session.execute(stmt)
        return result.scalar() or 0  # type: ignore[return-value]

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _to_observation(row: SecurityPrice) -> PriceObservation:
        """Convert a SecurityPrice ORM row to a PriceObservation DTO."""
        return PriceObservation(
            security_id=row.security_id,
            timestamp=row.timestamp,
            price_open=_to_decimal(row.price_open),
            price_high=_to_decimal(row.price_high),
            price_low=_to_decimal(row.price_low),
            price_close=_to_decimal(row.price_close),
            volume=_to_decimal(row.volume),
            source=row.source,
            interval=row.interval,
            currency_code=row.currency_code,
        )


def _to_decimal(value: Decimal | None) -> Decimal | None:
    """Convert a value to Decimal if it's not None."""
    if value is None:
        return None
    return Decimal(str(value))
