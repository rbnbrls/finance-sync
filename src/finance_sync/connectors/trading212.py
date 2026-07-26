"""Trading212 API connector implementation.

Uses Trading212's v0 REST API with API-key authentication.
The API key is sent directly as the ``Authorization`` header value
(without a ``Bearer`` prefix, per Trading212's convention).

Rate limit
    Trading212's free-tier API allows 10 requests per minute per API key.
    The connector's built-in
    :class:`~finance_sync.connectors.rate_limiter.RateLimiter` enforces
    this globally.

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
from typing import TYPE_CHECKING, Any

import httpx

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import RawAccount, RawTransaction
from finance_sync.connectors.rate_limiter import RateLimitPolicy

if TYPE_CHECKING:
    from finance_sync.connectors.models import ConnectorConfig

_T212_API_BASE_LIVE = "https://live.trading212.com"
_T212_API_BASE_DEMO = "https://demo.trading212.com"
_DEFAULT_PAGE_SIZE = 100


class Trading212Connector(Connector):
    """Connector for the Trading212 equity API (v0).

    Credentials
        ``config.credentials["api_key"]`` — Trading212 API key (required).
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

    rate_limit_policy = RateLimitPolicy(
        max_requests=10,
        window_seconds=60,
        max_retries=3,
        backoff_base=1.0,
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
        self._account_id: str | None = None
        self._account_currency: str = "EUR"

    @property
    def name(self) -> str:
        return "trading212"

    # ── Authentication ──────────────────────────────────────────────────

    async def authenticate(self) -> None:
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

        headers = _auth_headers(api_key)

        try:
            resp = await self._http.get(
                "/api/v0/equity/account/cash", headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
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
        headers = _auth_headers(api_key)
        try:
            resp = await self._http.get(
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
        headers = _auth_headers(api_key)

        try:
            resp = await self._http.get(
                "/api/v0/equity/portfolio", headers=headers
            )
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

    # ── Accounts ────────────────────────────────────────────────────────

    async def fetch_accounts(self) -> list[RawAccount]:
        """Return a single brokerage account for this Trading212 API key.

        Relies on ``_account_currency`` set during :meth:`authenticate`.
        """
        if not self._account_id:
            msg = "Trading212Connector not authenticated"
            raise PermanentError(msg)

        # Fetch fresh cash balance
        api_key = self.config.credentials.get("api_key", "")
        cash_data = await self._fetch_cash(api_key)

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
        headers = _auth_headers(api_key)
        try:
            resp = await self._http.get(
                "/api/v0/equity/account/cash", headers=headers
            )
            resp.raise_for_status()
            return resp.json()
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
    ) -> list[RawTransaction]:
        """Fetch orders and cash transactions since *since*.

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

        Returns:
            A combined, chronologically-sorted list of raw transactions.
        """
        if not self._account_id:
            msg = "Trading212Connector not authenticated"
            raise PermanentError(msg)

        # Reject account_id that doesn't match our single account
        if account_id is not None and account_id != self._account_id:
            return []

        api_key = self.config.credentials.get("api_key", "")

        # Fetch from both endpoints concurrently
        order_txns = await self._fetch_order_history(api_key, since, limit)
        cash_txns = await self._fetch_transaction_history(api_key, since, limit)

        all_txns: list[RawTransaction] = list(order_txns) + list(cash_txns)
        # Sort chronologically by occurred_at (most recent first)
        all_txns.sort(key=lambda t: t.occurred_at, reverse=True)

        if limit and len(all_txns) > limit:
            all_txns = all_txns[:limit]

        return all_txns

    async def _fetch_order_history(
        self,
        api_key: str,
        since: datetime,
        limit: int | None,
    ) -> list[RawTransaction]:
        """Fetch buy/sell order history with pagination."""
        items: list[RawTransaction] = []
        ps = min(limit, _DEFAULT_PAGE_SIZE) if limit else _DEFAULT_PAGE_SIZE
        path = f"/api/v0/equity/history/orders?limit={ps}"
        # Trading212 uses from/to query params in ISO-8601
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        path += f"&from={since_str}"

        headers = _auth_headers(api_key)

        while path:
            url = path
            try:
                resp = await self._http.get(url, headers=headers)
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
            for order in order_list:
                txn = _parse_order(order, self._account_id or "trading212")
                if txn.occurred_at >= since:
                    items.append(txn)
                    if limit and len(items) >= limit:
                        return items

            path = data.get("nextPagePath")

        return items

    async def _fetch_transaction_history(
        self,
        api_key: str,
        since: datetime,
        limit: int | None,
    ) -> list[RawTransaction]:
        """Fetch cash transaction history (dividends, deposits, etc.)
        with pagination."""
        items: list[RawTransaction] = []
        ps = min(limit, _DEFAULT_PAGE_SIZE) if limit else _DEFAULT_PAGE_SIZE
        path = f"/api/v0/equity/history/transactions?limit={ps}"
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        path += f"&from={since_str}"

        headers = _auth_headers(api_key)

        while path:
            url = path
            try:
                resp = await self._http.get(url, headers=headers)
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
            for txn_data in txn_list:
                txn = _parse_cash_transaction(
                    txn_data, self._account_id or "trading212"
                )
                if txn.occurred_at >= since:
                    items.append(txn)
                    if limit and len(items) >= limit:
                        return items

            path = data.get("nextPagePath")

        return items


# ── Module-level helpers ────────────────────────────────────────────────


def _auth_headers(api_key: str) -> dict[str, str]:
    """Return headers for Trading212 API requests.

    Trading212 expects the API key directly as the Authorization header
    value (no ``Bearer`` prefix).
    """
    return {
        "Authorization": api_key,
    }


def _raise_for_status(response: httpx.Response) -> None:
    """Raise appropriate connector error from an HTTP error response."""
    status = response.status_code
    if status == 429:
        retry_after = _parse_retry_after(response)
        msg = "Trading212 rate limit exceeded"
        raise RateLimitError(msg, retry_after=retry_after)
    if status in (401, 403):
        msg = f"Trading212 authentication failed (HTTP {status})"
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
            parsed = datetime.strptime(cleaned, fmt)  # noqa: DTZ007 — naive UTC from Trading212, tz attached below
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
        "TAX": "fee",
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
    filled_time = _parse_t212_datetime(data.get("filledTime"))
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

    return RawTransaction(
        external_transaction_id=f"order_{order_id}",
        external_account_id=account_id,
        amount=amount,
        currency_code=currency,
        occurred_at=creation_time,
        booked_at=filled_time or creation_time,
        description=f"{side} {quantity} x {ticker}"
        if ticker
        else f"{side} order {order_id}",
        transaction_type=_map_order_side(side),
        quantity=Decimal(str(quantity)) if quantity else None,
        status=_map_order_status(status_raw),
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
    txn_id = data.get("id", "")
    t212_type = data.get("type", "")
    amount = Decimal(str(data.get("amount", "0")))
    currency = data.get("currencyCode", "EUR")
    occurred_at = _parse_t212_datetime(data.get("dateTime"))
    reference = data.get("reference", "")
    ticker = data.get("ticker")

    # Dividends and inflows are positive; fees are negative
    canonical_type = _map_transaction_type(t212_type)
    if canonical_type in ("withdrawal", "fee"):
        amount = -abs(amount)
    else:
        amount = abs(amount)

    description = reference or f"{t212_type} transaction {txn_id}"
    if ticker:
        description = f"{ticker} {description}"

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
        provider_metadata={
            "t212_type": t212_type,
            "reference": reference,
            "ticker": ticker,
            "transaction_id": txn_id,
        },
    )
