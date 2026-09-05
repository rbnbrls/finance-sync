"""Trading212 API connector implementation.

Uses Trading212's v0 REST API with API-key authentication.
The API key is sent directly as the ``Authorization`` header value
(without a ``Bearer`` prefix, per Trading212's convention).

Rate limit
    Trading212 applies endpoint-specific limits per account.  The connector
    uses a conservative six-request/minute HTTP throttle and honours the
    provider's reset headers when the account is rate limited.

Pagination
    Trading212 uses cursor-based pagination via a ``nextPagePath`` field
    in paginated responses (``/history/orders`` and
    ``/history/transactions``).  The connector follows next-page URLs
    transparently.

Portfolio
    The connector provides a ``fetch_portfolio()`` method (not part of
    the abstract ``Connector`` base) that returns raw portfolio items
    with current holdings data.  The sync orchestration layer calls this
    separately and maps items to the ``Holdings`` model.

Dividends
    Dividends arrive via the ``/history/transactions`` endpoint as items
    with ``type: "DIVIDEND"``.  They are mapped to ``RawTransaction``
    objects with ``transaction_type="dividend"`` alongside regular
    buy/sell orders from ``/history/orders``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from time import time
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import (
    RawAccount,
    RawHolding,
    RawTransaction,
    SecurityReference,
)
from finance_sync.connectors.rate_limiter import RateLimiter, RateLimitPolicy

if TYPE_CHECKING:
    from finance_sync.connectors.models import ConnectorConfig

_T212_API_BASE_LIVE = "https://live.trading212.com"
_T212_API_BASE_DEMO = "https://demo.trading212.com"
# Trading212 caps the ``limit`` query param at 50 for both
# /history/orders and /history/transactions; anything larger returns
# HTTP 400 "Limit cannot be greater than 50" (see issue #505).
_DEFAULT_PAGE_SIZE = 50

# Trading212's portfolio endpoint returns broker-internal symbols.  Keep the
# provider identifier in metadata, but expose exchange-qualified symbols and
# known display names to the rest of the application.
_INSTRUMENT_ALIASES: dict[str, tuple[str, str, str]] = {
    "BESIA_EQ": ("BESI:XAMS", "BE Semiconductor Industries", "XAMS"),
}


def _normalise_instrument(
    ticker: str,
) -> tuple[str, str, str | None]:
    """Map a Trading212 internal ticker to a readable security reference."""
    key = ticker.upper()
    alias = _INSTRUMENT_ALIASES.get(key)
    if alias is not None:
        return alias

    # Dutch instruments are commonly returned as ``<symbol>a_EQ``.  The
    # suffix is Trading212's venue marker, not part of the public ticker.
    if key.endswith("A_EQ") and len(key) > 4:
        return f"{key[:-4]}:XAMS", ticker, "XAMS"
    return ticker, ticker, None


def _price_scale(ticker: str) -> Decimal:
    """Return the scale for Trading212's venue-specific quote units."""
    # London instruments are quoted in pence (GBX), while the account
    # endpoint reports the portfolio in the account currency.
    return Decimal("0.01") if ticker.upper().endswith("L_EQ") else Decimal(1)


class Trading212Connector(Connector):
    """Connector for the Trading212 equity API (v0).

    Credentials
        ``config.credentials["api_key"]`` — Trading212 API key (required).
        ``config.credentials["api_secret"]`` — API secret for current
        key-pair authentication (legacy single-key auth remains supported).
        ``config.options["demo"]`` — If ``True``, use the demo API base
        URL (default: ``False``).
        ``config.options["base_url"]`` — Custom API base URL (optional,
        overrides live/demo selection).

    Example::

        config = ConnectorConfig(
            provider_type="trading212",
            credentials={"api_key": "t212_api_key_abc123"},
            options={"demo": False},
        )
        conn = Trading212Connector(config)
        await conn.authenticate()
        portfolio = await conn.fetch_portfolio()
        txns = await conn.fetch_transactions(since=...)
    """

    display_name = "Trading212"
    sdk_version = "0.1.0"
    supported_resources = frozenset({"accounts", "transactions", "holdings"})
    # Trading212 is a historical broker API.  New connections should import
    # more than the generic platform lookback so the Wealthfolio account is
    # complete from the first sync.  Pagination and the endpoint limiter keep
    # this bounded by the provider's rate limits.
    initial_sync_lookback_days: ClassVar[int] = 3650

    rate_limit_policy = RateLimitPolicy(
        max_requests=6,
        window_seconds=60,
        max_retries=2,
        backoff_base=5.0,
    )

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the Trading212 connector.

        Args:
            config: Connector configuration with credentials.
            http_client: Optional pre-configured HTTP client (for testing).
        """
        super().__init__(config)

        if "base_url" in config.options:
            base_url = config.options["base_url"]
        elif config.options.get("demo", False):
            base_url = _T212_API_BASE_DEMO
        else:
            base_url = _T212_API_BASE_LIVE

        self._http = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0),
        )
        # Injected transports are used by deterministic unit tests; real
        # clients get endpoint-aware throttling.
        throttle_requests = 1 if http_client is None else 0
        self._http_rate_limiters = {
            "account": RateLimiter(
                RateLimitPolicy(
                    max_requests=throttle_requests,
                    window_seconds=5,
                    max_retries=0,
                )
            ),
            "portfolio": RateLimiter(
                RateLimitPolicy(
                    max_requests=throttle_requests,
                    window_seconds=1,
                    max_retries=0,
                )
            ),
            "metadata": RateLimiter(
                RateLimitPolicy(
                    max_requests=throttle_requests,
                    window_seconds=50,
                    max_retries=0,
                )
            ),
            "history": RateLimiter(
                RateLimitPolicy(
                    max_requests=6 if http_client is None else 0,
                    window_seconds=60,
                    max_retries=0,
                )
            ),
        }
        self._account_id: str | None = None
        self._account_currency: str = "EUR"
        self._cash_data: dict[str, Any] | None = None
        self._instrument_metadata: dict[str, dict[str, Any]] | None = None

    async def _get(
        self, url: str, *, headers: dict[str, str]
    ) -> httpx.Response:
        """Make a request after acquiring the endpoint's provider slot."""
        if "/equity/account/" in url:
            limiter = self._http_rate_limiters["account"]
        elif "/equity/portfolio" in url:
            limiter = self._http_rate_limiters["portfolio"]
        elif "/equity/metadata/" in url:
            limiter = self._http_rate_limiters["metadata"]
        else:
            limiter = self._http_rate_limiters["history"]
        await limiter.acquire()
        return await self._http.get(url, headers=headers)

    @property
    def name(self) -> str:
        return "trading212"

    # ── Authentication ──────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Authenticate with operation-level retry and reset-aware backoff."""
        self._cash_data = None
        if self._rate_limiter is not None:
            await self._rate_limiter.retry(self._authenticate_once)
        else:
            await self._authenticate_once()

    async def _authenticate_once(self) -> None:
        """Validate the Trading212 API key by calling
        ``GET /api/v0/equity/account/cash``.

        Raises:
            PermanentError: If the API key is missing or invalid.
            RateLimitError: If the Trading212 rate limit is exceeded.
            TransientError: On temporary provider unavailability.
        """
        api_key = self.config.credentials.get("api_key")
        if not api_key:
            msg = "Trading212 api_key is required in credentials"
            raise PermanentError(msg)

        headers = _auth_headers(
            api_key, self.config.credentials.get("api_secret")
        )

        try:
            resp = await self._get(
                "/api/v0/equity/account/cash", headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            self._cash_data = data
            self._account_currency = data.get("currencyCode", "EUR")
            # Account info for a more stable account identifier
            await self._load_account_info(api_key)
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
        except httpx.TimeoutException as exc:
            msg = "Trading212 authentication timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"Trading212 HTTP error during authenticate: {exc}"
            raise TransientError(msg) from exc

    async def _load_account_info(self, api_key: str) -> None:
        """Fetch account info to populate account ID and currency."""
        headers = _auth_headers(
            api_key, self.config.credentials.get("api_secret")
        )
        try:
            resp = await self._get(
                "/api/v0/equity/account/info", headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            self._account_id = str(data.get("id", ""))
            if "currencyCode" in data:
                self._account_currency = data["currencyCode"]
        except httpx.HTTPStatusError:
            # Non-critical — fall back to account/cash currency
            self._account_id = "trading212"
        except httpx.HTTPError:
            self._account_id = "trading212"

    # ── Portfolio ───────────────────────────────────────────────────────

    async def fetch_portfolio(self) -> list[dict[str, Any]]:
        """Fetch current portfolio holdings.

        Returns a list of raw portfolio items as returned by
        ``GET /api/v0/equity/portfolio``.

        Each item contains: ticker, quantity, averagePrice, currentPrice,
        initialFillDate, frontend, ppl data, etc.

        Raises:
            PermanentError: If not authenticated.
            TransientError: On API errors.
        """
        if not self._account_id:
            msg = "Trading212Connector not authenticated"
            raise PermanentError(msg)

        api_key = self.config.credentials.get("api_key", "")
        headers = _auth_headers(
            api_key, self.config.credentials.get("api_secret")
        )

        try:
            resp = await self._get("/api/v0/equity/portfolio", headers=headers)
            resp.raise_for_status()
            return resp.json()  # list of portfolio items
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
            raise  # unreachable
        except httpx.TimeoutException as exc:
            msg = "Trading212 portfolio request timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"Trading212 HTTP error fetching portfolio: {exc}"
            raise TransientError(msg) from exc

    async def fetch_instruments(self) -> list[dict[str, Any]]:
        """Fetch Trading212's instrument master for ISIN/name matching.

        The portfolio and history endpoints expose broker symbols such as
        ``BESIA_EQ`` but omit stable identifiers.  The metadata endpoint is
        the provider-of-record for ISIN, display name, venue and currency.
        Cache it for the lifetime of a sync so one sync consumes one metadata
        request, even though holdings and transactions are fetched separately.
        """
        if self._instrument_metadata is not None:
            return list(self._instrument_metadata.values())
        api_key = self.config.credentials.get("api_key", "")
        headers = _auth_headers(
            api_key, self.config.credentials.get("api_secret")
        )
        try:
            resp = await self._get(
                "/api/v0/equity/metadata/instruments", headers=headers
            )
            resp.raise_for_status()
            payload = resp.json()
            instruments = payload if isinstance(payload, list) else []
            self._instrument_metadata = {
                str(
                    item.get("ticker") or item.get("symbol") or ""
                ).upper(): item
                for item in instruments
                if isinstance(item, dict)
                and (item.get("ticker") or item.get("symbol"))
            }
            return instruments
        except httpx.HTTPStatusError as exc:
            # Older/demo Trading212 API deployments may not expose this
            # optional endpoint. Keep the data sync usable in that case.
            if exc.response.status_code == 404:
                self._instrument_metadata = {}
                return []
            _raise_for_status(exc.response)
            raise  # unreachable
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            msg = "Trading212 instrument metadata request failed"
            raise TransientError(msg) from exc

    async def fetch_holdings(
        self, *, account_id: str | None = None
    ) -> list[RawHolding]:
        """Map Trading212's live portfolio to canonical raw snapshots."""
        if not self._account_id:
            msg = "Trading212Connector not authenticated"
            raise PermanentError(msg)
        if account_id is not None and account_id != self._account_id:
            return []

        observed_at = datetime.now(UTC)
        instruments = await self.fetch_instruments()
        by_ticker = {
            str(item.get("ticker") or item.get("symbol") or "").upper(): item
            for item in instruments
            if isinstance(item, dict)
        }
        items = await self.fetch_portfolio()
        holdings: list[RawHolding] = []
        for item in items:
            # Normalise missing/null optional fields gracefully: never
            # let a provider-side null crash the whole holdings fetch or
            # leak the literal string "None" into the datamodel.
            ticker_raw = item.get("ticker")
            ticker = str(ticker_raw).strip() if ticker_raw is not None else ""
            instrument = by_ticker.get(ticker.upper(), {})
            public_ticker, display_name, venue = _normalise_instrument(ticker)
            metadata_isin = _metadata_value(instrument, "isin", "ISIN")
            metadata_name = _metadata_value(instrument, "name", "shortName")
            metadata_ticker = _metadata_value(instrument, "ticker", "symbol")
            metadata_venue = _metadata_value(
                instrument, "exchange", "exchangeCode", "venue"
            )
            quantity = _safe_quantity(item.get("quantity"))
            scale = _price_scale(ticker)
            average_price = _optional_decimal(item.get("averagePrice"))
            if average_price is not None:
                average_price *= scale
            current_price = _optional_decimal(item.get("currentPrice"))
            if current_price is not None:
                current_price *= scale
            currency = str(
                item.get("currencyCode")
                or instrument.get("currencyCode")
                or instrument.get("currency")
                or self._account_currency
            )
            frontend = str(item.get("frontend") or "").upper()
            security_type = "etf" if frontend == "ETF" else "stock"
            holdings.append(
                RawHolding(
                    external_account_id=self._account_id,
                    observed_at=observed_at,
                    quantity=quantity,
                    security_reference=SecurityReference(
                        # Use the public exchange-qualified symbol as the
                        # canonical lookup key.  This lets an existing
                        # security be reused after the broker's internal
                        # ``*_EQ`` identifier is normalised.
                        external_id=ticker or None,
                        ticker=(
                            metadata_ticker or public_ticker or ticker or None
                        ),
                        name=(
                            metadata_name
                            or (
                                display_name
                                if display_name != ticker
                                else None
                            )
                            or str(item.get("name") or display_name or None)
                        ),
                        isin=metadata_isin,
                        venue=metadata_venue or venue,
                        currency_code=currency,
                        security_type=security_type,
                    ),
                    cost_basis=(average_price * quantity)
                    if average_price is not None
                    else None,
                    cost_basis_currency=currency,
                    market_value=(current_price * quantity)
                    if current_price is not None
                    else None,
                    currency_code=currency,
                    price=current_price,
                    price_currency=currency,
                    provider_metadata={
                        "initial_fill_date": item.get("initialFillDate"),
                        "frontend": item.get("frontend"),
                        "trading212_ticker": ticker,
                    },
                )
            )
        return holdings

    # ── Accounts ────────────────────────────────────────────────────────

    async def fetch_accounts(self) -> list[RawAccount]:
        """Return a single brokerage account for this Trading212 API key.

        Relies on ``_account_currency`` set during :meth:`authenticate`.
        """
        if not self._account_id:
            msg = "Trading212Connector not authenticated"
            raise PermanentError(msg)

        # Reuse the cash response from authenticate() for this sync run.
        api_key = self.config.credentials.get("api_key", "")
        cash_data = self._cash_data or await self._fetch_cash(api_key)

        return [
            RawAccount(
                external_account_id=self._account_id,
                name="Trading212",
                account_type="brokerage",
                account_subtype=None,
                currency_code=self._account_currency,
                current_balance=cash_data.get("free"),
                available_balance=cash_data.get("free"),
                iso_currency_code=None,
                provider_metadata={
                    "invested": cash_data.get("invested"),
                    "result": cash_data.get("result"),
                    "blocked": cash_data.get("blocked"),
                    "pending": cash_data.get("pending"),
                    "pie_cash": cash_data.get("pieCash"),
                    "account_id": self._account_id,
                },
            )
        ]

    async def _fetch_cash(self, api_key: str) -> dict[str, Any]:
        """Fetch cash balance from the account/cash endpoint."""
        headers = _auth_headers(
            api_key, self.config.credentials.get("api_secret")
        )
        try:
            resp = await self._get(
                "/api/v0/equity/account/cash", headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            self._cash_data = data
            return data
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
            raise  # unreachable
        except httpx.TimeoutException as exc:
            msg = "Trading212 cash balance request timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"Trading212 HTTP error fetching cash: {exc}"
            raise TransientError(msg) from exc

    # ── Transactions ────────────────────────────────────────────────────

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
        to: datetime | None = None,
    ) -> list[RawTransaction]:
        """Fetch orders and cash transactions since *since* (optionally
        up to *to*).

        Combines data from two Trading212 endpoints:

        1. ``/api/v0/equity/history/orders`` — buy/sell orders.
        2. ``/api/v0/equity/history/transactions`` — dividends,
           deposits, withdrawals, interest, fees.

        When *account_id* is provided it is used as a filter on the
        returned transaction's ``external_account_id`` (the Trading212
        account is always a single brokerage account, so the filter is
        effectively a no-op for valid account IDs and returns empty
        for mismatched IDs).

        Args:
            since: Only return transactions occurring on or after this time.
            account_id: If set, only return transactions matching this
                account ID.
            limit: Maximum number of transactions to return.
            to: Optional exclusive upper bound; only return transactions
                occurring before this time (Trading212's ``to`` query
                parameter).  When omitted, the provider returns all
                transactions up to the present.

        Returns:
            A combined, chronologically-sorted list of raw transactions,
            deduplicated by ``external_transaction_id``.
        """
        if not self._account_id:
            msg = "Trading212Connector not authenticated"
            raise PermanentError(msg)

        # Reject account_id that doesn't match our single account
        if account_id is not None and account_id != self._account_id:
            return []

        if to is not None and to <= since:
            msg = (
                f"Trading212 date range invalid: 'to' ({to.isoformat()}) "
                f"must be after 'since' ({since.isoformat()})"
            )
            raise PermanentError(msg)

        api_key = self.config.credentials.get("api_key", "")

        # Fetch from both endpoints concurrently
        order_txns = await self._fetch_order_history(
            api_key, since, limit, to=to
        )
        cash_txns = await self._fetch_transaction_history(
            api_key, since, limit, to=to
        )

        all_txns: list[RawTransaction] = list(order_txns) + list(cash_txns)
        await self.fetch_instruments()
        all_txns = [self._enrich_transaction_security(txn) for txn in all_txns]
        # Deduplicate by provider external id (orders are prefixed
        # ``order_``, cash transactions ``txn_``; a same-id collision
        # between the two lists would otherwise double-persist).
        seen: set[str] = set()
        deduped: list[RawTransaction] = []
        for txn in all_txns:
            key = txn.external_transaction_id
            if key in seen:
                continue
            seen.add(key)
            deduped.append(txn)

        # Sort chronologically by occurred_at (most recent first)
        deduped.sort(key=lambda t: t.occurred_at, reverse=True)

        if limit and len(deduped) > limit:
            deduped = deduped[:limit]

        return deduped

    def _enrich_transaction_security(
        self, transaction: RawTransaction
    ) -> RawTransaction:
        """Add Trading212 instrument-master identifiers to a history row."""
        reference = transaction.security_reference
        if reference is None or not reference.ticker:
            return transaction
        item = (self._instrument_metadata or {}).get(
            reference.ticker.upper(), {}
        )
        if not item:
            return transaction
        isin = _metadata_value(item, "isin", "ISIN") or reference.isin
        name = _metadata_value(item, "name", "shortName") or reference.name
        venue = _metadata_value(
            item, "exchange", "exchangeCode", "venue"
        ) or reference.venue
        currency = (
            _metadata_value(item, "currencyCode", "currency")
            or reference.currency_code
        )
        enriched = reference.model_copy(
            update={
                "isin": isin,
                "name": name,
                "venue": venue,
                "currency_code": currency,
            }
        )
        return transaction.model_copy(update={"security_reference": enriched})

    async def _fetch_order_history(
        self,
        api_key: str,
        since: datetime,
        limit: int | None,
        to: datetime | None = None,
    ) -> list[RawTransaction]:
        """Fetch buy/sell order history with pagination."""
        items: list[RawTransaction] = []
        ps = min(limit, _DEFAULT_PAGE_SIZE) if limit else _DEFAULT_PAGE_SIZE
        path = f"/api/v0/equity/history/orders?limit={ps}"
        seen_paths: set[str] = set()
        # Trading212 uses from/to query params in ISO-8601
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        path += f"&from={since_str}"
        if to is not None:
            to_str = to.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            path += f"&to={to_str}"

        headers = _auth_headers(
            api_key, self.config.credentials.get("api_secret")
        )

        while path:
            if path in seen_paths:
                break
            seen_paths.add(path)
            url = path
            try:
                resp = await self._get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc.response)
                raise  # unreachable
            except httpx.TimeoutException as exc:
                msg = "Trading212 order history request timed out"
                raise TransientError(msg) from exc
            except httpx.HTTPError as exc:
                msg = f"Trading212 HTTP error fetching orders: {exc}"
                raise TransientError(msg) from exc

            order_list: list[dict[str, Any]] = data.get("items", [])
            oldest_in_page: datetime | None = None
            for order in order_list:
                txn = _parse_order(order, self._account_id or "trading212")
                if oldest_in_page is None or txn.occurred_at < oldest_in_page:
                    oldest_in_page = txn.occurred_at
                if txn.occurred_at >= since:
                    items.append(txn)
                    if limit and len(items) >= limit:
                        return items

            if oldest_in_page is not None and oldest_in_page < since:
                break
            path = data.get("nextPagePath")

        return items

    async def _fetch_transaction_history(
        self,
        api_key: str,
        since: datetime,
        limit: int | None,
        to: datetime | None = None,
    ) -> list[RawTransaction]:
        """Fetch cash transaction history (dividends, deposits, etc.)
        with pagination."""
        items: list[RawTransaction] = []
        ps = min(limit, _DEFAULT_PAGE_SIZE) if limit else _DEFAULT_PAGE_SIZE
        path = f"/api/v0/equity/history/transactions?limit={ps}"
        seen_paths: set[str] = set()
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        path += f"&from={since_str}"
        if to is not None:
            to_str = to.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            path += f"&to={to_str}"

        headers = _auth_headers(
            api_key, self.config.credentials.get("api_secret")
        )

        while path:
            if path in seen_paths:
                break
            seen_paths.add(path)
            url = path
            try:
                resp = await self._get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc.response)
                raise  # unreachable
            except httpx.TimeoutException as exc:
                msg = "Trading212 transaction history request timed out"
                raise TransientError(msg) from exc
            except httpx.HTTPError as exc:
                msg = f"Trading212 HTTP error fetching transactions: {exc}"
                raise TransientError(msg) from exc

            txn_list: list[dict[str, Any]] = data.get("items", [])
            oldest_in_page: datetime | None = None
            for txn_data in txn_list:
                txn = _parse_cash_transaction(
                    txn_data, self._account_id or "trading212"
                )
                if oldest_in_page is None or txn.occurred_at < oldest_in_page:
                    oldest_in_page = txn.occurred_at
                if txn.occurred_at >= since:
                    items.append(txn)
                    if limit and len(items) >= limit:
                        return items

            if oldest_in_page is not None and oldest_in_page < since:
                break
            path = data.get("nextPagePath")

        return items


# ── Module-level helpers ────────────────────────────────────────────────


def _auth_headers(
    api_key: str, api_secret: str | None = None
) -> dict[str, str]:
    """Return headers for Trading212 API requests.

    Current Trading212 keys use HTTP Basic with an API key/secret pair.
    Legacy single-part keys remain supported for existing configurations.
    """
    if api_secret:
        import base64

        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode(
            "ascii"
        )
        return {"Authorization": f"Basic {token}"}
    return {
        "Authorization": api_key,
    }


def _raise_for_status(response: httpx.Response) -> None:
    """Raise appropriate connector error from an HTTP error response.

    Classification mirrors the provider contract:

    - ``429`` → :class:`RateLimitError` (retryable, honours ``Retry-After``).
    - ``401``/``403`` → :class:`PermanentError` (bad/expired credentials).
    - Other ``4xx`` (e.g. ``400`` invalid ``from``/``since``, ``404`` unknown
      resource) → :class:`PermanentError` — a client error will not resolve
      by retrying the same request.
    - ``5xx`` → :class:`TransientError` (temporary provider outage).
    """
    status = response.status_code
    if status == 429:
        retry_after = _parse_retry_after(response)
        reset_at = _parse_rate_limit_reset(response)
        if reset_at is not None:
            reset_delay = max(0.0, reset_at - time()) + 1.0
            retry_after = max(retry_after or 0.0, reset_delay)
        msg = "Trading212 rate limit exceeded"
        raise RateLimitError(msg, retry_after=retry_after)
    if status in (401, 403):
        msg = f"Trading212 authentication failed (HTTP {status})"
        raise PermanentError(msg)
    if 400 <= status < 500:
        msg = f"Trading212 request failed (HTTP {status})"
        raise PermanentError(msg)
    msg = f"Trading212 request failed (HTTP {status})"
    raise TransientError(msg)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Extract ``Retry-After`` header value in seconds."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_rate_limit_reset(response: httpx.Response) -> float | None:
    """Return the provider's reset epoch from ``x-ratelimit-reset``."""
    value = response.headers.get("x-ratelimit-reset")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_t212_datetime(raw: str | None) -> datetime:
    """Parse a Trading212 ISO-8601 timestamp to a UTC-aware datetime.

    Trading212 formats::

        "2024-01-15T10:00:00.000Z"
        "2024-01-15T10:00:00Z"
    """
    if not raw:
        return datetime.fromtimestamp(0, tz=UTC)

    # Strip trailing 'Z' and parse
    cleaned = raw.rstrip("Z")
    if not cleaned:
        return datetime.fromtimestamp(0, tz=UTC)

    # Try with milliseconds first, then without
    parsed: datetime | None = None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        return datetime.fromtimestamp(0, tz=UTC)

    return parsed.replace(tzinfo=UTC)


def _map_order_side(side: str) -> str:
    """Map Trading212 order side to canonical transaction type."""
    mapping = {
        "BUY": "purchase",
        "SELL": "sale",
    }
    return mapping.get(side.upper(), "other")


def _map_order_status(status: str) -> str:
    """Map Trading212 order status to canonical status."""
    mapping = {
        "FILLED": "booked",
        "PENDING": "pending",
        "CANCELLED": "cancelled",
        "REJECTED": "cancelled",
        "PARTIALLY_FILLED": "pending",
    }
    return mapping.get(status.upper(), "pending")


def _map_transaction_type(t212_type: str) -> str:
    """Map Trading212 cash transaction type to canonical type."""
    mapping = {
        "DIVIDEND": "dividend",
        "DEPOSIT": "deposit",
        "WITHDRAWAL": "withdrawal",
        "INTEREST": "interest",
        "FEE": "fee",
        "TAX": "tax",
        "CASHBACK": "deposit",
        "LOYALTY_BONUS": "interest",
    }
    return mapping.get(t212_type.upper(), "other")


def _parse_order(
    data: dict[str, Any],
    account_id: str,
) -> RawTransaction:
    """Map a Trading212 order JSON object to a RawTransaction."""
    order_id = data.get("id", "")
    ticker = data.get("ticker", "")
    side = data.get("side", "")
    total = Decimal(str(data.get("total", "0")))
    currency = data.get("currencyCode", "EUR")
    filled_time_raw = data.get("filledTime")
    filled_time = (
        _parse_t212_datetime(filled_time_raw) if filled_time_raw else None
    )
    creation_time = _parse_t212_datetime(data.get("creationTime"))
    status_raw = data.get("status", "")
    filled_price = data.get("filledPrice")
    quantity = data.get("filledQuantity") or data.get("quantity", 0)
    tax = data.get("tax", 0)
    stamp_duty = data.get("stampDuty", 0)
    execution_venue = data.get("executionVenue")
    order_type = data.get("type", "")

    # Amount is outflow (negative) for buys, inflow (positive) for sells
    amount = -total if side.upper() == "BUY" else total

    # Fees: Trading212 reports tax + stamp duty separately; both reduce
    # the net cash flow of the order.
    fee_total = (Decimal(str(tax or 0))) + (Decimal(str(stamp_duty or 0)))

    return RawTransaction(
        external_transaction_id=f"order_{order_id}",
        external_account_id=account_id,
        amount=amount,
        currency_code=currency,
        # A filled order occurred at execution time. For pending orders
        # Trading212 leaves filledTime null; retain creation time instead of
        # converting the missing timestamp to the Unix epoch.
        occurred_at=filled_time or creation_time,
        booked_at=filled_time or creation_time,
        description=f"{side} {quantity} x {ticker}"
        if ticker
        else f"{side} order {order_id}",
        transaction_type=_map_order_side(side),
        quantity=Decimal(str(quantity)) if quantity else None,
        unit_price=(
            Decimal(str(filled_price)) if filled_price is not None else None
        ),
        fee_amount=fee_total or None,
        fee_currency_code=currency if fee_total else None,
        status=_map_order_status(status_raw),
        security_reference=SecurityReference(
            external_id=ticker or None,
            ticker=ticker or None,
            name=ticker or None,
            venue=execution_venue,
            currency_code=currency,
            security_type=(
                "etf"
                if str(data.get("frontend", "")).upper() == "ETF"
                else "stock"
            ),
        )
        if ticker
        else None,
        provider_metadata={
            "ticker": ticker,
            "order_type": order_type,
            "side": side,
            "filled_price": filled_price,
            "quantity": quantity,
            "tax": tax,
            "stamp_duty": stamp_duty,
            "execution_venue": execution_venue,
            "order_id": order_id,
        },
    )


def _parse_cash_transaction(
    data: dict[str, Any],
    account_id: str,
) -> RawTransaction:
    """Map a Trading212 cash transaction JSON object to a RawTransaction.

    Covers dividends, deposits, withdrawals, interest, and fees.
    """
    # The live API omits ``id`` for some cash-transaction types.  Falling
    # back to an empty string makes every such record look like ``txn_`` and
    # the connector-level deduplication then collapses the whole history to
    # one row.  ``reference`` is provider-generated for those responses; the
    # remaining fields make the fallback deterministic for older payloads.
    txn_id = data.get("id")
    if txn_id in (None, ""):
        txn_id = data.get("reference") or "|".join(
            str(data.get(field, ""))
            for field in (
                "type",
                "dateTime",
                "amount",
                "currencyCode",
                "ticker",
            )
        )
    t212_type = data.get("type", "")
    amount = Decimal(str(data.get("amount", "0")))
    currency = data.get("currencyCode", "EUR")
    occurred_at = _parse_t212_datetime(data.get("dateTime"))
    reference = data.get("reference", "")
    ticker = data.get("ticker")

    # Dividends and inflows are positive; fees are negative
    canonical_type = _map_transaction_type(t212_type)
    if canonical_type in ("withdrawal", "fee", "tax"):
        amount = -abs(amount)
    else:
        amount = abs(amount)

    description = reference or f"{t212_type} transaction {txn_id}"
    if ticker:
        description = f"{ticker} {description}"

    # For fee/tax cash transactions, surface the provider-reported
    # amount as a positive fee (mirrors how orders report tax/stamp duty).
    fee_amount = abs(amount) if canonical_type in ("fee", "tax") else None

    return RawTransaction(
        external_transaction_id=f"txn_{txn_id}",
        external_account_id=account_id,
        amount=amount,
        currency_code=currency,
        occurred_at=occurred_at,
        booked_at=occurred_at,
        description=description,
        transaction_type=canonical_type,
        status="booked",
        fee_amount=fee_amount,
        fee_currency_code=currency if fee_amount is not None else None,
        security_reference=SecurityReference(
            external_id=str(ticker),
            ticker=str(ticker),
            name=str(ticker),
            currency_code=currency,
        )
        if ticker
        else None,
        provider_metadata={
            "t212_type": t212_type,
            "reference": reference,
            "ticker": ticker,
            "transaction_id": txn_id,
        },
    )


def _optional_decimal(value: Any) -> Decimal | None:
    """Parse an optional provider number without turning null into zero."""
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _metadata_value(item: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty provider metadata value as text."""
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _safe_quantity(value: Any) -> Decimal:
    """Parse a holding quantity, defaulting to zero on null/missing.

    Trading212 reports ``quantity`` as a number, but defensive parsing
    keeps a malformed/null portfolio item from crashing the whole
    holdings fetch (quantity is a required ``RawHolding`` field).
    """
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))
