"""Market-data provider API for downstream applications.

The API is deliberately provider-agnostic at its boundary.  Trading212 is
the first live adapter; historical data is served from finance-sync's local
price store because Trading212 does not expose historical candles in the
portfolio API.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
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
from finance_sync.models.holding import Holding
from finance_sync.models.security import Security
from finance_sync.models.security_listing import SecurityListing
from finance_sync.services.auth import decrypt_credential

router = APIRouter(prefix="/market-data", tags=["market-data"])

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$", re.IGNORECASE)
_YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"


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
    requested = symbol.strip().upper()
    bare = requested.rsplit(":", 1)[-1]
    result = await db.execute(
        select(Security)
        .outerjoin(SecurityListing, SecurityListing.security_id == Security.id)
        .where(
            or_(
                func.upper(Security.ticker) == requested,
                func.upper(Security.isin) == requested,
                func.upper(SecurityListing.ticker) == requested,
                func.upper(SecurityListing.ticker) == bare,
            )
        )
        .limit(1)
    )
    return result.scalars().first()


async def _local_quote(
    db: AsyncSession,
    settings: Settings,
    auth: AuthContext,
    symbol: str,
) -> dict[str, Any]:
    """Return the tenant's latest authoritative quote for a security.

    Current holdings are preferred over the generic price cache because
    broker snapshots may contain a more authoritative closing price than a
    third-party enrichment feed.  The price cache remains the fallback for
    securities that are not currently held.
    """
    security = await _security(db, auth=auth, symbol=symbol)
    if security is None:
        raise HTTPException(
            status_code=404, detail=f"Security not found: {symbol}"
        )

    holding = (
        (
            await db.execute(
                select(Holding)
                .where(
                    Holding.tenant_id == auth.tenant_id,
                    Holding.security_id == security.id,
                    Holding.quantity > 0,
                )
                .order_by(Holding.observed_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if holding is not None:
        price = holding.price
        if (
            price is None
            and holding.market_value is not None
            and holding.quantity
        ):
            price = Decimal(holding.market_value) / Decimal(holding.quantity)
        if price is not None:
            return {
                "symbol": security.ticker or security.isin or symbol,
                "isin": security.isin,
                "price": float(price),
                "currency": (
                    holding.price_currency
                    or holding.currency_code
                    or security.currency_code
                ).upper(),
                "timestamp": holding.observed_at.isoformat(),
                "date": holding.observed_at.date().isoformat(),
                "source": f"finance-sync:{holding.source}",
            }

    observation = await PriceStore(db, settings).get_latest_price(
        str(security.id)
    )
    if observation is None or observation.price_close is None:
        raise HTTPException(
            status_code=404, detail=f"No quote available for {symbol}"
        )
    return {
        "symbol": security.ticker or security.isin or symbol,
        "isin": security.isin,
        "price": float(observation.price_close),
        "currency": (
            observation.currency_code or security.currency_code
        ).upper(),
        "timestamp": observation.timestamp.isoformat(),
        "date": observation.timestamp.date().isoformat(),
        "source": observation.source,
    }


async def _resolve_yahoo_symbol(identifier: str) -> str | None:
    """Resolve an ISIN or exchange-qualified symbol through Yahoo search.

    Wealthfolio commonly sends an ISIN when an imported asset has no ticker.
    The local cache cannot answer that request when the security was imported
    directly into Wealthfolio, so the custom provider needs a read-only
    identifier bridge.  An unresolved identifier deliberately returns None;
    it is never converted into a fabricated quote.
    """
    value = identifier.strip().upper()
    if not value:
        return None
    if not _ISIN_RE.fullmatch(value):
        return value
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                _YAHOO_SEARCH_URL,
                params={"q": value, "quotesCount": 10, "newsCount": 0},
                headers={"User-Agent": "finance-sync/market-data"},
            )
            response.raise_for_status()
            quotes = response.json().get("quotes", [])
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    for quote in quotes:
        symbol = str(quote.get("symbol") or "").strip()
        quote_type = str(quote.get("quoteType") or "").upper()
        if symbol and quote_type in {"EQUITY", "ETF", "MUTUALFUND", "INDEX"}:
            return symbol
    return None


async def _yahoo_chart(
    identifier: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[str, str | None, list[dict[str, Any]]] | None:
    """Fetch a daily Yahoo series for the custom-provider fallback."""
    yahoo_symbol = await _resolve_yahoo_symbol(identifier)
    if yahoo_symbol is None:
        return None
    params: dict[str, Any] = {"interval": "1d", "events": "history"}
    if start is not None:
        params["period1"] = int(start.timestamp())
    if end is not None:
        params["period2"] = int(end.timestamp()) + 86400
    else:
        # Latest-quote lookups must stay small; bounded historical requests
        # use period1/period2 above and can span the requested window.
        params["range"] = "5d"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(
                f"{_YAHOO_CHART_URL}/{yahoo_symbol}",
                params=params,
                headers={"User-Agent": "finance-sync/market-data"},
            )
            response.raise_for_status()
            raw_response = cast(dict[str, Any], response.json())
            chart = cast(dict[str, Any], raw_response.get("chart") or {})
            result = cast(list[dict[str, Any]], chart.get("result") or [])
            if not result:
                return None
            payload: dict[str, Any] = result[0]
            meta = cast(dict[str, Any], payload.get("meta") or {})
            timestamps = cast(list[int], payload.get("timestamp") or [])
            indicators = cast(dict[str, Any], payload.get("indicators") or {})
            quotes = cast(list[dict[str, Any]], indicators.get("quote") or [])
            quote: dict[str, Any] = quotes[0] if quotes else {}
            currency = cast(str | None, meta.get("currency"))
            rows: list[dict[str, Any]] = []
            for index, timestamp in enumerate(timestamps):
                close = (quote.get("close") or [None])[index]
                if close is None:
                    continue
                rows.append(
                    {
                        "date": datetime.fromtimestamp(
                            timestamp, tz=UTC
                        ).isoformat(),
                        "price": float(close),
                        "open": _float_at(quote.get("open"), index),
                        "high": _float_at(quote.get("high"), index),
                        "low": _float_at(quote.get("low"), index),
                        "volume": _float_at(quote.get("volume"), index),
                        "currency": currency,
                        "source": "yahoo",
                    }
                )
            return yahoo_symbol, currency, rows
    except (httpx.HTTPError, ValueError, TypeError, IndexError):
        return None


def _float_at(values: Any, index: int) -> float | None:
    if not isinstance(values, list):
        return None
    values_list = cast(list[Any], values)
    if index >= len(values_list):
        return None
    value = values_list[index]
    return float(value) if value is not None else None


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
    """Return one quote, suitable for Wealthfolio JSONPath ``$.price``.

    The generic endpoint serves finance-sync's canonical holdings/price data
    first and falls back to Trading212 live data for compatibility with the
    original provider-specific integration.
    """
    if connection_id:
        return await _live_quote(db, settings, auth, symbol, connection_id)
    try:
        local = await _local_quote(db, settings, auth, symbol)
        local_timestamp = datetime.fromisoformat(str(local["timestamp"]))
        if (datetime.now(UTC) - local_timestamp).total_seconds() <= getattr(
            settings, "price_cache_ttl_seconds", 86400
        ):
            return local
        external = await _yahoo_chart(symbol)
        if external and external[2]:
            row = external[2][-1]
            return {
                "symbol": symbol,
                "price": row["price"],
                "currency": row.get("currency") or local["currency"],
                "timestamp": row["date"],
                "date": row["date"][:10],
                "source": "yahoo",
            }
        return local
    except HTTPException as local_error:
        if local_error.status_code != 404:
            raise
        external = await _yahoo_chart(symbol)
        if external and external[2]:
            row = external[2][-1]
            return {
                "symbol": symbol,
                "price": row["price"],
                "currency": row.get("currency") or "EUR",
                "timestamp": row["date"],
                "date": row["date"][:10],
                "source": "yahoo",
            }
        return await _live_quote(db, settings, auth, symbol, None)


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
    local_rows = [
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
            "volume": float(item.volume) if item.volume is not None else None,
            "currency": item.currency_code,
            "source": item.source,
        }
        for item in observations
    ]
    external = await _yahoo_chart(symbol, start=date_from, end=date_to)
    merged = {str(item["date"])[:10]: item for item in local_rows}
    if external:
        _, _, external_rows = external
        for item in external_rows:
            merged.setdefault(str(item["date"])[:10], item)
    rows = sorted(merged.values(), key=lambda item: str(item["date"]))
    return {
        "symbol": symbol,
        "data": rows,
        "currency": next(
            (
                str(item.get("currency")).upper()
                for item in rows
                if item.get("currency")
            ),
            None,
        ),
        "coverage": "local-cache+yahoo" if external else "local-cache",
        "historical_source_available": external is not None,
    }
