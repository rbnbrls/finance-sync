"""EnrichmentGateway — OpenBB-powered market data enrichment gateway.

Provides:
- Security metadata resolution (ISIN, ticker, FIGI → full instrument details)
- Historical price data (daily, hourly, minute)
- Latest quotes for portfolio valuation
- Rate-limit and authentication management
- Graceful degradation when OpenBB is unavailable
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings
    from finance_sync.db.uow import UnitOfWork
    from finance_sync.enrichment.price_store import PriceStore

import httpx

from finance_sync.enrichment.models import (
    ETFComposition,
    ETFHolding,
    FundamentalObservationData,
    PriceObservation,
    QuoteResult,
    RegionExposure,
    ResolvedSecurity,
    SectorExposure,
)
from finance_sync.models.enrichment_freshness import EnrichmentFreshness


class EnrichmentGateway:
    """Gateway for market data enrichment via OpenBB.

    Uses the OpenBB Platform REST API to resolve securities and
    fetch pricing data. Operates in a degraded mode when the
    OpenBB API key is not configured (returns local-only results).
    """

    def __init__(
        self,
        settings: Settings,
        uow: UnitOfWork,
        price_store: PriceStore,
    ) -> None:
        self._settings = settings
        self._uow = uow
        self._price_store = price_store

        self._http_client: httpx.AsyncClient | None = None
        self._degraded = settings.openbb_api_key is None

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy-init HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self._settings.openbb_base_url,
                timeout=httpx.Timeout(self._settings.openbb_request_timeout),
                headers=self._build_headers(),
            )
        return self._http_client

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for OpenBB API requests."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "finance-sync/0.1.0",
        }
        if self._settings.openbb_api_key:
            api_key = self._settings.openbb_api_key.get_secret_value()
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    # ── Degraded mode ────────────────────────────────────────────────────

    @property
    def is_degraded(self) -> bool:
        """Whether the gateway is operating in degraded mode
        (no OpenBB API key configured)."""
        return self._degraded

    # ── Security Resolution ──────────────────────────────────────────────

    async def resolve_security(
        self,
        identifier: str,
        identifier_type: str,
    ) -> ResolvedSecurity | None:
        """Resolve a security by identifier via OpenBB.

        Args:
            identifier: The identifier value (ISIN, ticker, FIGI, name).
            identifier_type: Type of the identifier
                ('isin', 'ticker', 'figi', 'name').

        Returns:
            A ResolvedSecurity on success, or None if not found /
            degraded mode.
        """
        if self._degraded:
            return None

        try:
            response = await self.http_client.get(
                f"/api/{self._settings.openbb_api_version}/market/security",
                params={"identifier": identifier, "type": identifier_type},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return self._parse_resolved_security(data)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            if exc.response.status_code == 429:
                # Rate limited — return None instead of blocking
                return None
            return None
        except httpx.TimeoutException:
            return None
        except httpx.HTTPError:
            return None

    @staticmethod
    def _parse_resolved_security(data: dict[str, Any]) -> ResolvedSecurity:
        """Parse an OpenBB security response into a ResolvedSecurity."""
        return ResolvedSecurity(
            security_id=str(uuid4()),
            isin=data.get("isin"),
            figi=data.get("figi"),
            ticker=data.get("ticker") or data.get("symbol"),
            name=data.get("name", "Unknown"),
            currency_code=data.get("currency", "EUR"),
            confidence="exact",
            source="openbb",
        )

    # ── Quote ────────────────────────────────────────────────────────────

    async def get_latest_quote(
        self,
        security_id: str,
        identifier: str,
        identifier_type: str = "ticker",
    ) -> QuoteResult | None:
        """Fetch the latest quote for a security via OpenBB.

        Falls back to local price data if OpenBB is unavailable.
        """
        # Try OpenBB first
        if not self._degraded:
            try:
                quote = await self._fetch_openbb_quote(
                    identifier, identifier_type
                )
                if quote is not None:
                    await self._store_quote_result(security_id, quote)
                    return quote
            except httpx.HTTPError:
                pass

        # Fall back to local PriceStore
        observation = await self._price_store.get_latest_price(
            security_id=security_id,
            interval="1d",
        )
        if observation is not None and observation.price_close is not None:
            return QuoteResult(
                security_id=security_id,
                symbol=identifier,
                price=observation.price_close,
                currency_code=observation.currency_code,
                timestamp=observation.timestamp,
                source="local",
            )

        return None

    async def _fetch_openbb_quote(
        self,
        identifier: str,
        identifier_type: str,
    ) -> QuoteResult | None:
        """Fetch a real-time quote from OpenBB."""
        response = await self.http_client.get(
            f"/api/{self._settings.openbb_api_version}/market/quote",
            params={"symbol": identifier, "type": identifier_type},
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        return QuoteResult(
            security_id="",  # caller fills this in
            symbol=identifier,
            price=Decimal(str(data.get("price", 0))),
            change=_safe_decimal(data.get("change")),
            change_pct=_safe_decimal(data.get("changePercent")),
            currency_code=data.get("currency", "EUR"),
            timestamp=datetime.now(UTC),
            source="openbb",
        )

    async def _store_quote_result(
        self,
        security_id: str,
        quote: QuoteResult,
    ) -> None:
        """Persist a quote result as a price observation."""
        await self._price_store.store_prices(
            [
                PriceObservation(
                    security_id=security_id,
                    timestamp=quote.timestamp,
                    price_close=quote.price,
                    interval="1d",
                    currency_code=quote.currency_code,
                    source=quote.source,
                )
            ]
        )

    # ── Fundamentals ────────────────────────────────────────────────────

    async def get_fundamentals(
        self,
        identifier: str,
        identifier_type: str = "ticker",
    ) -> FundamentalObservationData | None:
        """Fetch fundamental ratio data for a security from OpenBB.

        Returns the fundamental observation data or None if
        degraded / unavailable.
        """
        if self._degraded:
            return None

        try:
            response = await self.http_client.get(
                f"/api/{self._settings.openbb_api_version}/market/fundamentals",
                params={
                    "symbol": identifier,
                    "type": identifier_type,
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return self._parse_fundamentals(data)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 429):
                return None
            return None
        except (httpx.TimeoutException, httpx.HTTPError):
            return None

    @staticmethod
    def _parse_fundamentals(
        data: dict[str, Any],
    ) -> FundamentalObservationData:
        """Parse an OpenBB fundamentals response into a
        FundamentalObservationData DTO.
        """
        from datetime import UTC, datetime

        return FundamentalObservationData(
            security_id="",
            timestamp=datetime.now(UTC),
            pe_ratio=_safe_decimal(data.get("peRatio") or data.get("pe_ratio")),
            forward_pe=_safe_decimal(
                data.get("forwardPE") or data.get("forward_pe")
            ),
            peg_ratio=_safe_decimal(
                data.get("pegRatio") or data.get("peg_ratio")
            ),
            eps=_safe_decimal(data.get("eps") or data.get("trailingEps")),
            eps_forward=_safe_decimal(
                data.get("forwardEps") or data.get("eps_forward")
            ),
            book_value_per_share=_safe_decimal(
                data.get("bookValue") or data.get("book_value_per_share")
            ),
            dividend_yield=_safe_decimal(
                data.get("dividendYield") or data.get("dividend_yield")
            ),
            dividend_rate=_safe_decimal(
                data.get("dividendRate") or data.get("dividend_rate")
            ),
            market_cap=_safe_decimal(
                data.get("marketCap") or data.get("market_cap")
            ),
            enterprise_value=_safe_decimal(
                data.get("enterpriseValue") or data.get("enterprise_value")
            ),
            shares_outstanding=_safe_decimal(
                data.get("sharesOutstanding") or data.get("shares_outstanding")
            ),
            beta=_safe_decimal(data.get("beta")),
            high_52w=_safe_decimal(
                data.get("high52w")
                or data.get("fiftyTwoWeekHigh")
                or data.get("high_52w")
            ),
            low_52w=_safe_decimal(
                data.get("low52w")
                or data.get("fiftyTwoWeekLow")
                or data.get("low_52w")
            ),
            source="openbb",
            provider_metadata={
                k: v
                for k, v in data.items()
                if k
                not in (
                    "peRatio",
                    "pe_ratio",
                    "forwardPE",
                    "forward_pe",
                    "eps",
                    "marketCap",
                    "market_cap",
                    "dividendYield",
                    "beta",
                )
            },
        )

    # ── ETF Composition ─────────────────────────────────────────────────

    async def get_etf_composition(
        self,
        identifier: str,
        identifier_type: str = "ticker",
    ) -> ETFComposition | None:
        """Fetch ETF composition (holdings, sector/region exposures)
        from OpenBB.

        Returns None in degraded mode or for non-ETF securities.
        """
        if self._degraded:
            return None

        try:
            response = await self.http_client.get(
                f"/api/{self._settings.openbb_api_version}"
                "/market/etf/composition",
                params={
                    "symbol": identifier,
                    "type": identifier_type,
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return self._parse_etf_composition(data, symbol=identifier)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 429):
                return None
            return None
        except (httpx.TimeoutException, httpx.HTTPError):
            return None

    def _parse_etf_composition(
        self, data: dict[str, Any], symbol: str = "Unknown ETF"
    ) -> ETFComposition:
        """Parse an OpenBB ETF composition response into an
        ETFComposition DTO.
        """
        holdings_raw = data.get("holdings") or data.get("topHoldings") or []
        sector_raw = (
            data.get("sectorExposures")
            or data.get("sector_exposures")
            or data.get("sectorWeights")
            or []
        )
        region_raw = (
            data.get("regionExposures") or data.get("region_exposures") or []
        )

        holdings = [
            ETFHolding(
                ticker=h.get("ticker") or h.get("symbol"),
                name=h.get("name") or h.get("description"),
                weight=_safe_decimal(h.get("weight") or h.get("percentage")),
                sector=h.get("sector"),
                market_value=_safe_decimal(
                    h.get("marketValue") or h.get("market_value")
                ),
                shares=_safe_decimal(h.get("shares")),
            )
            for h in holdings_raw
        ]

        sector_exposures = [
            SectorExposure(
                sector=s.get("sector") or s.get("name") or s.get("industry"),
                weight=_safe_decimal(
                    s.get("weight") or s.get("exposure") or s.get("percentage")
                )
                or Decimal(0),
            )
            for s in sector_raw
            if (s.get("sector") or s.get("name"))
        ]

        region_exposures = [
            RegionExposure(
                region=r.get("region") or r.get("name"),
                weight=_safe_decimal(
                    r.get("weight") or r.get("exposure") or r.get("percentage")
                )
                or Decimal(0),
            )
            for r in region_raw
            if (r.get("region") or r.get("name"))
        ]

        return ETFComposition(
            etf_name=data.get("name")
            or data.get("etfName")
            or data.get("etf_name")
            or symbol,
            total_holdings=data.get("totalHoldings")
            or data.get("total_holdings")
            or len(holdings),
            holdings=holdings,
            sector_exposures=sector_exposures,
            region_exposures=region_exposures,
            expense_ratio=_safe_decimal(
                data.get("expenseRatio")
                or data.get("expense_ratio")
                or data.get("expenseRatio")
            ),
            dividend_yield=_safe_decimal(
                data.get("dividendYield") or data.get("dividend_yield")
            ),
            source="openbb",
        )

    # ── Historical Prices ────────────────────────────────────────────────

    async def get_historical_prices(
        self,
        security_id: str,
        identifier: str,
        identifier_type: str = "ticker",
        interval: str = "1d",
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 100,
    ) -> list[PriceObservation]:
        """Fetch historical prices, checking local cache first.

        Returns cached data if fresh enough, otherwise fetches from
        OpenBB and stores the results.
        """
        # Check if we have local data first
        local = await self._price_store.get_price_history(
            security_id=security_id,
            interval=interval,
            start=start_date,
            end=end_date,
            limit=limit,
        )
        if local and len(local) >= limit:
            return local

        # Fetch from OpenBB
        if self._degraded:
            return local  # return whatever we had locally

        try:
            observations = await self._fetch_openbb_history(
                identifier=identifier,
                identifier_type=identifier_type,
                interval=interval,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
            # Re-assign security_id
            for obs in observations:
                obs.security_id = security_id

            # Store new observations (deduplication handled by PriceStore)
            await self._price_store.store_prices(observations)

            # Return combined results
            return await self._price_store.get_price_history(
                security_id=security_id,
                interval=interval,
                start=start_date,
                end=end_date,
                limit=limit,
            )

        except httpx.HTTPError:
            return local  # fall back to local

    async def _fetch_openbb_history(
        self,
        identifier: str,
        identifier_type: str,
        interval: str = "1d",
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 100,
    ) -> list[PriceObservation]:
        """Fetch historical OHLCV data from OpenBB."""
        params: dict[str, Any] = {
            "symbol": identifier,
            "type": identifier_type,
            "interval": interval,
            "limit": limit,
        }
        if start_date is not None:
            params["start_date"] = start_date.isoformat()
        if end_date is not None:
            params["end_date"] = end_date.isoformat()

        response = await self.http_client.get(
            f"/api/{self._settings.openbb_api_version}/market/history",
            params=params,
        )
        response.raise_for_status()
        data: list[dict[str, Any]] = response.json()

        return [
            PriceObservation(
                security_id="",
                timestamp=_parse_timestamp(
                    item.get("date") or item.get("timestamp")
                ),
                price_open=_safe_decimal(item.get("open")),
                price_high=_safe_decimal(item.get("high")),
                price_low=_safe_decimal(item.get("low")),
                price_close=_safe_decimal(item.get("close")),
                volume=_safe_decimal(item.get("volume")),
                source="openbb",
                interval=interval,
                currency_code=item.get("currency", "EUR"),
                provider_metadata=item.get("provider_metadata"),
            )
            for item in data
        ]

    # ── Enrichment Freshness ─────────────────────────────────────────────

    async def update_freshness(
        self,
        security_id: str,
        field: str,
        *,
        status: str = "resolved",
        error_message: str | None = None,
    ) -> None:
        """Update the enrichment freshness record for a security."""
        # Find existing record
        records = await self._uow.enrichment_freshness.list(
            EnrichmentFreshness.security_id == security_id  # type: ignore[attr-defined]
        )
        now = datetime.now(UTC)

        if records:
            record = records[0]
            setattr(record, field, now)
            record.status = status
            if error_message:
                record.error_message = error_message
            await self._uow.enrichment_freshness.update(record)
        else:
            kwargs: dict[str, Any] = {
                "security_id": security_id,
                "data_source": "openbb" if not self._degraded else "local",
                "status": status,
            }
            kwargs[field] = now
            if error_message:
                kwargs["error_message"] = error_message
            record = EnrichmentFreshness(**kwargs)
            await self._uow.enrichment_freshness.add(record)

    # ── Cleanup ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()


# ── Module-level helpers ─────────────────────────────────────────────────


def _safe_decimal(value: Any) -> Decimal | None:
    """Safely convert a value to Decimal, returning None on failure."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return None


def _parse_timestamp(raw: str | None) -> datetime:
    """Parse an ISO-8601 timestamp string to a UTC-aware datetime."""
    if not raw:
        return datetime.now(UTC)
    try:
        # Try with Z suffix
        cleaned = raw.rstrip("Z")
        if not cleaned:
            return datetime.now(UTC)
        return datetime.fromisoformat(cleaned).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return datetime.now(UTC)
