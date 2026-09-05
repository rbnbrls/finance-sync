"""Security and security-price read component."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from finance_sync.models.security import Security
from finance_sync.models.security_listing import SecurityListing
from finance_sync.models.security_price import SecurityPrice
from finance_sync.schemas.freshness import freshness_for
from finance_sync.services.read.pagination import expression
from finance_sync.services.read.prices import fetch_latest_daily_prices
from finance_sync.services.read.schemas import (
    CollectionMeta,
    SecurityInfo,
    SecurityListResponse,
    SecurityPriceListResponse,
    SecurityPriceResponse,
    TopLevelPriceListResponse,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class SecuritiesReadService:
    """Read securities, latest prices and historical price observations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_securities(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        security_type: str | None = None,
        search: str | None = None,
    ) -> Any:
        conditions: list[Any] = []
        if security_type is not None:
            conditions.append(Security.security_type == security_type)
        if search is not None:
            pattern = f"%{search}%"
            conditions.append(
                or_(
                    Security.name.ilike(pattern),
                    Security.ticker.ilike(pattern),
                    Security.isin.ilike(pattern),
                    Security.figi.ilike(pattern),
                )
            )
        total_result = await self._session.execute(
            select(func.count())
            .select_from(Security)
            .where(expression(*conditions))
        )
        total: int = total_result.scalar() or 0
        result = await self._session.execute(
            select(Security)
            .where(expression(*conditions))
            .order_by(Security.name.asc())
            .offset(offset)
            .limit(limit)
        )
        rows: list[Security] = list(result.scalars().all())
        price_map = await fetch_latest_daily_prices(
            self._session, [str(security.id) for security in rows]
        )
        items: list[Any] = []
        for security in rows:
            price = price_map.get(str(security.id))
            items.append(
                SecurityInfo(
                    id=str(security.id),
                    isin=security.isin,
                    figi=security.figi,
                    ticker=security.ticker,
                    name=security.name,
                    security_type=str(security.security_type),
                    currency_code=security.currency_code,
                    latest_price=price.price_close if price else None,
                    latest_price_currency=(
                        price.currency_code if price else None
                    ),
                    latest_price_timestamp=price.timestamp if price else None,
                    created_at=security.created_at,
                    updated_at=security.updated_at,
                )
            )
        return SecurityListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_security_prices(
        self,
        security_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        interval: str = "1d",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> Any:
        conditions: list[Any] = [
            SecurityPrice.security_id == security_id,
            SecurityPrice.interval == interval,
        ]
        if date_from is not None:
            conditions.append(SecurityPrice.timestamp >= date_from)
        if date_to is not None:
            conditions.append(SecurityPrice.timestamp <= date_to)
        total_result = await self._session.execute(
            select(func.count())
            .select_from(SecurityPrice)
            .where(expression(*conditions))
        )
        total: int = total_result.scalar() or 0
        result = await self._session.execute(
            select(SecurityPrice)
            .where(expression(*conditions))
            .order_by(SecurityPrice.timestamp.desc())
            .offset(offset)
            .limit(limit)
        )
        rows: list[SecurityPrice] = list(result.scalars().all())
        return SecurityPriceListResponse(
            items=[
                SecurityPriceResponse(
                    id=str(price.id),
                    security_id=str(price.security_id),
                    timestamp=price.timestamp,
                    price_open=price.price_open,
                    price_high=price.price_high,
                    price_low=price.price_low,
                    price_close=price.price_close,
                    volume=price.volume,
                    source=price.source,
                    interval=price.interval,
                    currency_code=price.currency_code,
                    venue=price.venue,
                )
                for price in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def resolve_listing_security_id(self, listing_id: str) -> str | None:
        result = await self._session.execute(
            select(SecurityListing.security_id).where(
                SecurityListing.id == listing_id
            )
        )
        return result.scalar_one_or_none()

    async def get_prices(
        self,
        *,
        security_id: str | None = None,
        interval: str = "1d",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Any:
        if security_id is not None:
            conditions: list[Any] = [
                SecurityPrice.security_id == security_id,
                SecurityPrice.interval == interval,
            ]
            if date_from is not None:
                conditions.append(SecurityPrice.timestamp >= date_from)
            if date_to is not None:
                conditions.append(SecurityPrice.timestamp <= date_to)
            meta_row = (
                await self._session.execute(
                    select(
                        func.count().label("total"),
                        func.max(SecurityPrice.timestamp).label("as_of"),
                    )
                    .select_from(SecurityPrice)
                    .where(expression(*conditions))
                )
            ).one()
            total: int = meta_row.total or 0
            as_of = meta_row.as_of
        else:
            latest = (
                select(
                    SecurityPrice.security_id,
                    func.max(SecurityPrice.timestamp).label("latest_ts"),
                )
                .where(SecurityPrice.interval == interval)
                .group_by(SecurityPrice.security_id)
                .subquery()
            )
            conditions = [
                SecurityPrice.interval == interval,
                SecurityPrice.security_id == latest.c.security_id,
                SecurityPrice.timestamp == latest.c.latest_ts,
            ]
            count_result = await self._session.execute(
                select(func.count(func.distinct(SecurityPrice.security_id)))
                .select_from(SecurityPrice)
                .where(expression(*conditions))
            )
            total = count_result.scalar() or 0
            as_of = None

        result = await self._session.execute(
            select(SecurityPrice)
            .where(expression(*conditions))
            .order_by(SecurityPrice.timestamp.desc())
            .offset(offset)
            .limit(limit)
        )
        rows: list[SecurityPrice] = list(result.scalars().all())
        if as_of is None:
            as_of = max((price.timestamp for price in rows), default=None)
        return TopLevelPriceListResponse(
            items=[
                SecurityPriceResponse(
                    id=str(price.id),
                    security_id=str(price.security_id),
                    timestamp=price.timestamp,
                    price_open=price.price_open,
                    price_high=price.price_high,
                    price_low=price.price_low,
                    price_close=price.price_close,
                    volume=price.volume,
                    source=price.source,
                    interval=price.interval,
                    currency_code=price.currency_code,
                )
                for price in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
            meta=CollectionMeta(
                as_of=as_of,
                freshness=freshness_for(as_of),
            ),
        )
