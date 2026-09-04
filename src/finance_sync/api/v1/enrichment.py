"""Enrichment status endpoint — coverage, freshness, and health."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
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
from finance_sync.enrichment.models import (
    EnrichmentStatusSummary,
    PriceObservation,
)
from finance_sync.enrichment.price_store import PriceStore
from finance_sync.models.credential import Credential
from finance_sync.models.enrichment_freshness import EnrichmentFreshness
from finance_sync.models.holding import Holding
from finance_sync.models.market_data_exception import MarketDataException
from finance_sync.models.security import Security
from finance_sync.models.security_listing import SecurityListing
from finance_sync.services.auth import decrypt_credential

router = APIRouter(tags=["enrichment"])


def _options(credential: Credential) -> dict[str, Any]:
    try:
        value = json.loads(credential.description or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return {key: value for key, value in value.items() if key != "_label"}


def _ticker_variants(value: object) -> set[str]:
    raw = str(value or "").strip().upper()
    if not raw:
        return set()
    return {raw, raw.rsplit(":", 1)[-1]}


@router.post("/enrichment/refresh-quotes")
async def refresh_quotes(
    auth: AuthContext = Depends(require_permission("enrichment", "write")),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Refresh stale securities through active broker API connections.

    File imports are intentionally never treated as live quote providers.
    Trading212 is currently the supported quote-capable broker adapter.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    accepted_ids = set(
        (
            await session.execute(
                select(MarketDataException.security_id).where(
                    MarketDataException.tenant_id == auth.tenant_id
                )
            )
        ).scalars()
    )
    stale_result = await session.execute(
        select(Security)
        .join(Holding, Holding.security_id == Security.id)
        .outerjoin(
            EnrichmentFreshness,
            EnrichmentFreshness.security_id == Security.id,
        )
        .where(
            Holding.tenant_id == auth.tenant_id,
            ~Security.id.in_(accepted_ids) if accepted_ids else True,
            (EnrichmentFreshness.last_quote_fetch.is_(None))
            | (EnrichmentFreshness.last_quote_fetch < cutoff)
        )
    )
    securities = list(stale_result.scalars().all())
    credentials = list(
        (
            await session.execute(
                select(Credential).where(
                    Credential.tenant_id == auth.tenant_id,
                    Credential.provider_key == "trading212",
                    Credential.status == "active",
                )
            )
        ).scalars()
    )
    if not credentials:
        return {
            "status": "unavailable",
            "message": "Geen actieve koers-API connector beschikbaar.",
            "updated": 0,
            "unmatched": len(securities),
            "providers": [],
        }

    updated = 0
    matched_ids: set[str] = set()
    providers: list[str] = []
    for credential in credentials:
        raw = decrypt_credential(
            credential.encrypted_payload, credential.nonce, settings
        )
        try:
            credentials_data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            credentials_data = {"api_key": raw}
        connector = ConnectorRegistry().get_connector(
            ConnectorConfig(
                provider_type="trading212",
                credentials=credentials_data,
                options=_options(credential),
                connection_id=str(credential.id),
            )
        )
        try:
            await connector.authenticate()
            portfolio = cast(list[dict[str, Any]], await connector.fetch_portfolio())
            # Trading212's portfolio payload uses internal tickers.  Its
            # instrument master supplies the stable ISIN/name/currency used
            # to match imported DEGIRO/Saxo securities safely.
            try:
                instruments = cast(
                    list[dict[str, Any]], await connector.fetch_instruments()
                )
            except Exception:
                instruments = []
            providers.append("trading212")
            by_ticker = {
                variant: item
                for item in portfolio
                for variant in _ticker_variants(item.get("ticker"))
            }
            by_isin = {
                str(item.get("isin", "")).strip().upper(): item
                for item in instruments
                if item.get("isin")
            }
            instrument_by_ticker = {
                variant: item
                for item in instruments
                for variant in _ticker_variants(item.get("ticker"))
            }
            observations: list[PriceObservation] = []
            observed_at = datetime.now(UTC)
            freshness_rows: dict[str, EnrichmentFreshness] = {}
            for security in securities:
                instrument = by_isin.get(str(security.isin or "").upper())
                if instrument is None:
                    instrument = next(
                        (
                            instrument_by_ticker.get(variant)
                            for variant in _ticker_variants(security.ticker)
                        ),
                        None,
                    )
                item = (
                    next(
                        (
                            by_ticker.get(variant)
                            for variant in _ticker_variants(
                                instrument.get("ticker")
                            )
                        ),
                        None,
                    )
                    if instrument is not None
                    else next(
                        (
                            by_ticker.get(variant)
                            for variant in _ticker_variants(security.ticker)
                        ),
                        None,
                    )
                )
                price = item.get("currentPrice") if item else None
                if price is None:
                    continue
                security_id = str(security.id)
                raw_ticker = str(item.get("ticker", ""))
                scale = _price_scale(raw_ticker)
                price = Decimal(str(price)) * scale
                currency = str(
                    item.get("currencyCode")
                    or (instrument or {}).get("currencyCode")
                    or security.currency_code
                    or "EUR"
                )
                _, _, venue = _normalise_instrument(raw_ticker)
                instrument_isin = str((instrument or {}).get("isin", "")).upper()
                if instrument is not None and instrument_isin == str(
                    security.isin or ""
                ).upper():
                    instrument_name = str(
                        instrument.get("name")
                        or instrument.get("shortName")
                        or ""
                    ).strip()
                    normalized_ticker, _, normalized_venue = (
                        _normalise_instrument(raw_ticker)
                    )
                    if instrument_name and (
                        not security.name
                        or security.name == security.ticker
                        or security.name == security.isin
                        or security.name.upper().endswith("_EQ")
                    ):
                        security.name = instrument_name
                    if normalized_ticker and (
                        not security.ticker
                        or security.ticker.upper().endswith("_EQ")
                    ):
                        security.ticker = normalized_ticker
                    if normalized_venue:
                        listing = await session.scalar(
                            select(SecurityListing).where(
                                SecurityListing.security_id == security.id,
                                SecurityListing.mic == normalized_venue,
                                SecurityListing.currency_code == currency.upper(),
                            )
                        )
                        if listing is None:
                            session.add(
                                SecurityListing(
                                    security_id=security.id,
                                    mic=normalized_venue,
                                    ticker=normalized_ticker,
                                    currency_code=currency.upper(),
                                    is_primary_listing=True,
                                )
                            )
                observations.append(
                    PriceObservation(
                        security_id=security_id,
                        timestamp=observed_at,
                        price_close=price,
                        source="trading212",
                        interval="1d",
                        currency_code=currency,
                        venue=venue,
                    )
                )
                matched_ids.add(security_id)
                freshness_rows[security_id] = await session.scalar(
                    select(EnrichmentFreshness).where(
                        EnrichmentFreshness.security_id == security_id
                    )
                )
            await PriceStore(session, settings).store_prices(observations)
            for security_id, freshness in freshness_rows.items():
                if freshness is None:
                    freshness = EnrichmentFreshness(security_id=security_id)
                    session.add(freshness)
                freshness.last_quote_fetch = observed_at
                freshness.data_source = "trading212"
                freshness.status = "resolved"
                freshness.error_message = None
                updated += 1
        finally:
            close = getattr(connector, "close", None)
            if close is not None:
                await close()
    await session.flush()
    return {
        "status": "completed" if updated else "partial",
        "updated": updated,
        "unmatched": len(securities) - len(matched_ids),
        "providers": sorted(set(providers)),
    }


@router.post("/enrichment/accept-unavailable-quotes")
async def accept_unavailable_quotes(
    auth: AuthContext = Depends(require_permission("enrichment", "write")),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Accept missing quotes as an intentional exception for this tenant."""
    result = await session.execute(
        select(Security)
        .join(Holding, Holding.security_id == Security.id)
        .outerjoin(
            EnrichmentFreshness,
            EnrichmentFreshness.security_id == Security.id,
        )
        .where(
            Holding.tenant_id == auth.tenant_id,
            ~Security.id.in_(
                select(MarketDataException.security_id).where(
                    MarketDataException.tenant_id == auth.tenant_id
                )
            ),
            EnrichmentFreshness.last_quote_fetch.is_(None),
        )
        .distinct()
    )
    securities = list(result.scalars())
    for security in securities:
        freshness = await session.scalar(
            select(EnrichmentFreshness).where(
                EnrichmentFreshness.security_id == security.id
            )
        )
        if freshness is None:
            freshness = EnrichmentFreshness(security_id=str(security.id))
            session.add(freshness)
        freshness.status = "unavailable_accepted"
        freshness.error_message = (
            "Gebruiker accepteerde dat marktdata niet beschikbaar is."
        )
        freshness.data_source = "user_accepted"
        session.add(
            MarketDataException(
                tenant_id=auth.tenant_id, security_id=str(security.id)
            )
        )
    await session.flush()
    return {"status": "completed", "accepted": len(securities)}


@router.get("/enrichment/status")
async def get_enrichment_status(
    _auth: AuthContext = Depends(require_permission("enrichment", "read")),
    session: AsyncSession = Depends(get_db),
) -> EnrichmentStatusSummary:
    """Return enrichment coverage and freshness statistics.

    Shows how many securities have been enriched, how many are
    pending, stale, or failed, and the last enrichment timestamp.
    """
    # Total securities
    total_result = await session.execute(
        select(func.count()).select_from(Security)
    )
    total_securities: int = total_result.scalar() or 0  # type: ignore[assignment]

    # Count by freshness status
    status_counts: dict[str, int] = {}
    status_query = await session.execute(
        select(
            EnrichmentFreshness.status,
            func.count(),
        ).group_by(EnrichmentFreshness.status)
    )
    for row in status_query:
        status_counts[str(row[0])] = int(row[1])

    # Securities with at least one price
    prices_result = await session.execute(
        select(func.count(func.distinct(EnrichmentFreshness.security_id)))  # type: ignore[attr-defined]
    )
    enriched: int = prices_result.scalar() or 0  # type: ignore[assignment]

    # Stale: enriched but not updated in the last 24h
    stale_cutoff = datetime.now(UTC) - timedelta(hours=24)
    stale_result = await session.execute(
        select(func.count())
        .select_from(EnrichmentFreshness)
        .where(
            EnrichmentFreshness.last_quote_fetch < stale_cutoff,  # type: ignore[attr-defined]
        )
    )
    stale: int = stale_result.scalar() or 0  # type: ignore[assignment]

    # Last enrichment run timestamp
    latest_result = await session.execute(
        select(func.max(EnrichmentFreshness.updated_at))
    )
    last_enrichment: datetime = latest_result.scalar()  # type: ignore[assignment]

    # Active data sources
    sources_result = await session.execute(
        select(func.distinct(EnrichmentFreshness.data_source))
    )
    data_sources: list[str] = [str(row[0]) for row in sources_result]

    return EnrichmentStatusSummary(
        total_securities=total_securities,
        enriched_securities=enriched,
        pending_securities=status_counts.get("pending", 0),
        failed_securities=status_counts.get("failed", 0),
        stale_securities=stale,
        last_enrichment_run=last_enrichment,
        data_sources=data_sources or ["openbb"],
    )
