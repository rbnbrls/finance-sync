"""Market-data provider API for downstream applications.

The API is deliberately provider-agnostic at its boundary.  Trading212 is
the first live adapter; historical data is served from finance-sync's local
price store because Trading212 does not expose historical candles in the
portfolio API.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.config.settings import Settings
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.connectors.trading212 import (
    _normalise_instrument,
    _price_scale,
)
from finance_sync.dependencies import get_db, get_settings
from finance_sync.enrichment.models import PriceObservation
from finance_sync.enrichment.price_store import PriceStore
from finance_sync.models.credential import Credential
from finance_sync.models.security import Security
from finance_sync.services.auth import decrypt_credential

router = APIRouter(prefix="/market-data", tags=["market-data"])


def _parse_options(credential: Credential) -> dict[str, Any]:
    try:
        value = json.loads(credential.description or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in value.items() if k != "_label"}


async def _credentials(
    db: AsyncSession,
    *,
    auth: AuthContext,
    connection_id: str | None,
) -> list[Credential]:
    conditions: list[Any] = [
        Credential.tenant_id == auth.tenant_id,
        Credential.provider_key == "trading212",
        Credential.status == "active",
    ]
    if connection_id:
        conditions.append(Credential.id == connection_id)
    result = await db.execute(select(Credential).where(*conditions))
    rows = list(result.scalars().all())
    if connection_id and not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trading212 connection not found",
        )
    return rows


async def _security(
    db: AsyncSession, *, auth: AuthContext, symbol: str
) -> Security | None:
    result = await db.execute(
        select(Security).where(
            or_(Security.ticker == symbol, Security.isin == symbol)
        )
    )
    return result.scalars().first()


async def _live_quote(
    db: AsyncSession,
    settings: Settings,
    auth: AuthContext,
    symbol: str,
    connection_id: str | None,
) -> dict[str, Any]:
    credentials = await _credentials(db, auth=auth, connection_id=connection_id)
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active Trading212 connection configured",
        )

    wanted = symbol.strip().upper()
    for credential in credentials:
        raw = decrypt_credential(
            credential.encrypted_payload, credential.nonce, settings
        )
        try:
            secret_values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            secret_values = {"api_key": raw}
        config = ConnectorConfig(
            provider_type="trading212",
            credentials=secret_values,
            options=_parse_options(credential),
            connection_id=str(credential.id),
        )
        connector = ConnectorRegistry().get_connector(config)
        try:
            await connector.authenticate()
            portfolio = cast(
                list[dict[str, Any]],
                await connector.fetch_portfolio(),  # type: ignore[attr-defined]
            )
            item = next(
                (
                    row
                    for row in portfolio
                    if str(row.get("ticker", "")).upper() == wanted
                ),
                None,
            )
            if item is None:
                continue
            price = item.get("currentPrice")
            if price is None:
                continue
            observed_at = datetime.now(UTC)
            security = await _security(db, auth=auth, symbol=symbol)
            if security is not None:
                raw_ticker = str(item.get("ticker", symbol))
                price_value = Decimal(str(price)) * _price_scale(raw_ticker)
                _, _, venue = _normalise_instrument(raw_ticker)
                await PriceStore(db, settings).store_prices(
                    [
                        PriceObservation(
                            security_id=str(security.id),
                            timestamp=observed_at,
                            price_close=price_value,
                            source="trading212",
                            interval="1d",
                            currency_code=str(
                                item.get("currencyCode")
                                or security.currency_code
                                or "EUR"
                            ),
                            venue=venue,
                            provider_metadata={
                                "connection_id": str(credential.id),
                                "symbol": str(item.get("ticker", symbol)),
                            },
                        )
                    ]
                )
            return {
                "symbol": item.get("ticker", symbol),
                "price": float(price),
                "currency": item.get("currencyCode", "EUR"),
                "timestamp": observed_at.isoformat(),
                "source": "trading212",
                "connection_id": str(credential.id),
            }
        finally:
            close = getattr(connector, "close", None)
            if close is not None:
                await close()

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Trading212 has no open position for symbol {symbol!r}",
    )


@router.get("/latest")
async def latest_quote(
    symbol: str = Query(..., min_length=1, max_length=64),
    connection_id: str | None = Query(default=None),
    auth: AuthContext = Depends(require_permission("market-data", "read")),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return one current quote, suitable for Wealthfolio JSONPath ``$.price``."""
    return await _live_quote(db, settings, auth, symbol, connection_id)


@router.get("/history")
async def price_history(
    symbol: str = Query(..., min_length=1, max_length=64),
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=1000, ge=1, le=5000),
    auth: AuthContext = Depends(require_permission("market-data", "read")),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return locally cached observations; no synthetic broker history is made."""
    security = await _security(db, auth=auth, symbol=symbol)
    observations: list[PriceObservation] = []
    if security is not None:
        observations = await PriceStore(db, settings).get_price_history(
            str(security.id),
            interval="1d",
            start=date_from,
            end=date_to,
            limit=limit,
        )
    return {
        "symbol": symbol,
        "data": [
            {
                "date": item.timestamp.isoformat(),
                "price": float(item.price_close)
                if item.price_close is not None
                else None,
                "open": float(item.price_open)
                if item.price_open is not None
                else None,
                "high": float(item.price_high)
                if item.price_high is not None
                else None,
                "low": float(item.price_low)
                if item.price_low is not None
                else None,
                "volume": float(item.volume)
                if item.volume is not None
                else None,
                "currency": item.currency_code,
                "source": item.source,
            }
            for item in observations
        ],
        "coverage": "local-cache",
        "historical_source_available": False,
    }
