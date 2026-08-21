"""Set-based price queries shared by portfolio and security reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, select

from finance_sync.models.security_price import SecurityPrice

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def fetch_latest_daily_prices(
    session: AsyncSession,
    security_ids: list[str],
) -> dict[str, SecurityPrice]:
    """Fetch the latest daily price for all securities in one query."""
    if not security_ids:
        return {}
    ranked = (
        select(
            SecurityPrice.id.label("price_id"),
            func.row_number()
            .over(
                partition_by=SecurityPrice.security_id,
                order_by=SecurityPrice.timestamp.desc(),
            )
            .label("row_number"),
        )
        .where(
            SecurityPrice.security_id.in_(security_ids),  # type: ignore[attr-defined]
            SecurityPrice.interval == "1d",  # type: ignore[attr-defined]
        )
        .subquery()
    )
    result = await session.execute(
        select(SecurityPrice).join(
            ranked,
            and_(
                SecurityPrice.id == ranked.c.price_id,  # type: ignore[attr-defined]
                ranked.c.row_number == 1,
            ),
        )
    )
    return {
        str(price.security_id): price
        for price in result.scalars().all()  # type: ignore[assignment]
    }
