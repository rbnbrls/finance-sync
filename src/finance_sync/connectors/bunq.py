"""Bunq API connector implementation.

Uses bunq's v1 API with API-key authentication. The authentication flow
creates a session-server from the API key.  For production use, the full
installation flow (key exchange, device registration, session creation) is
needed; this implementation assumes an existing installation and performs
the session-server step on each :meth:`authenticate` call.

Rate limit
    bunq allows 60 requests per minute per user.  The connector's
    built-in :class:`~finance_sync.connectors.rate_limiter.RateLimiter`
    enforces this globally.

Pagination
    bunq uses cursor-based pagination via ``Pagination.future_url``.
    The connector follows next-page URLs transparently in
    ``fetch_accounts`` and ``fetch_transactions``.
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
    from collections.abc import Sequence

    from finance_sync.connectors.models import ConnectorConfig

_BUNQ_API_BASE = "https://api.bunq.com/v1"
_DEFAULT_COUNT = 200


class BunqConnector(Connector):
    """Connector for the bunq banking API (v1).

    Credentials
        ``config.credentials[\"api_key\"]`` — bunq API key (required).
        ``config.options[\"base_url\"]`` — custom API base URL (optional,
        for sandbox/testing).

    Example::

        config = ConnectorConfig(
            provider_type=\"bunq\",
            credentials={\"api_key\": \"bunq_api_key_abc123\"},
            options={\"sandbox\": True},
        )
        conn = BunqConnector(config)
        await conn.authenticate()
        accounts = await conn.fetch_accounts()
    """

    display_name = "Bunq"
    sdk_version = "0.1.0"

    rate_limit_policy = RateLimitPolicy(
        max_requests=60,
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
        """Initialise the bunq connector.

        Args:
            config: Connector configuration with credentials.
            http_client: Optional pre-configured HTTP client (for testing).
        """
        super().__init__(config)
        base_url = _BUNQ_API_BASE
        if "base_url" in config.options:
            base_url = config.options["base_url"]
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0),
        )
        self._session_token: str | None = None
        self._user_id: int | None = None

    @property
    def name(self) -> str:
        return "bunq"

    # ── Authentication ──────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Create a bunq session-server using the configured API key.

        Raises:
            PermanentError: If the API key is missing or invalid.
            RateLimitError: If the bunq rate limit is exceeded.
            TransientError: On temporary provider unavailability.
        """
        api_key = self.config.credentials.get("api_key")
        if not api_key:
            msg = "bunq api_key is required in credentials"
            raise PermanentError(msg)

        try:
            session_data = await self._create_session(api_key)
            self._session_token = session_data["token"]
            self._user_id = session_data["user_id"]
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
        except httpx.TimeoutException as exc:
            msg = "bunq session creation timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"bunq HTTP error during authenticate: {exc}"
            raise TransientError(msg) from exc

    async def _create_session(
        self,
        api_key: str,
    ) -> dict[str, Any]:
        """POST /session-server with the API key.

        Returns a dict with ``token`` and ``user_id``.
        """
        headers = _base_headers()
        body: dict[str, object] = {"secret": api_key}
        resp = await self._http.post(
            "/session-server", json=body, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

        session_token: str | None = None
        user_id: int | None = None

        for item in data.get("Response", []):
            if "Token" in item:
                session_token = item["Token"]["token"]
            if "UserPerson" in item:
                user_id = int(item["UserPerson"]["id"])
            if "UserCompany" in item:
                user_id = int(item["UserCompany"]["id"])

        if not session_token or not user_id:
            msg = "bunq session-server response missing token or user_id"
            raise PermanentError(msg)

        return {"token": session_token, "user_id": user_id}

    def _auth_headers(self) -> dict[str, str]:
        """Return request headers with the session token."""
        if not self._session_token:
            msg = "BunqConnector not authenticated — call authenticate() first"
            raise PermanentError(msg)

        headers = _base_headers()
        headers["X-Bunq-Client-Authentication"] = self._session_token
        return headers

    # ── Accounts ────────────────────────────────────────────────────────

    async def fetch_accounts(self) -> list[RawAccount]:
        """Fetch all monetary accounts (bank + savings) via paginated API."""
        if not self._user_id:
            msg = "BunqConnector not authenticated"
            raise PermanentError(msg)

        accounts: list[RawAccount] = []
        accounts.extend(
            await self._fetch_monetary_accounts("MonetaryAccountBank")
        )
        accounts.extend(
            await self._fetch_monetary_accounts("MonetaryAccountSavings")
        )
        return accounts

    async def _fetch_monetary_accounts(
        self,
        account_type: str,
    ) -> list[RawAccount]:
        """Fetch monetary accounts of a given type, handling pagination."""
        items: list[RawAccount] = []
        url = f"/user/{self._user_id}/monetary-account?count={_DEFAULT_COUNT}"

        while url:
            data = await self._request_paginated(url)
            for entry in data.get("Response", []):
                account_data = entry.get(account_type)
                if account_data is None:
                    continue
                items.append(self._parse_account(account_data, account_type))
            url = self._next_page_url(data)

        return items

    @staticmethod
    def _parse_account(
        data: dict[str, Any],
        bunq_type: str,
    ) -> RawAccount:
        """Map a bunq monetary-account JSON object to a RawAccount."""
        account_id = str(data["id"])
        description = data.get("description") or data.get("name", "")

        if bunq_type == "MonetaryAccountSavings":
            acct_type = "savings"
        elif bunq_type == "MonetaryAccountBank":
            acct_type = "checking"
        else:
            acct_type = "other"

        balance_data = data.get("balance", {})
        current_balance = (
            Decimal(balance_data["value"])
            if balance_data.get("value")
            else None
        )
        currency = balance_data.get("currency", "EUR")

        iban: str | None = None
        for alias in data.get("alias", []):
            if alias.get("type") == "IBAN":
                iban = alias.get("value")
                break

        return RawAccount(
            external_account_id=account_id,
            name=description,
            account_type=acct_type,
            account_subtype=None,
            currency_code=currency,
            current_balance=current_balance,
            available_balance=None,
            iso_currency_code=currency,
            provider_metadata={
                "bunq_type": bunq_type,
                "iban": iban,
                "status": data.get("status"),
                "sub_type": data.get("sub_type"),
            },
        )

    # ── Transactions ────────────────────────────────────────────────────

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Fetch payments for one or all accounts.

        When *account_id* is provided, only fetches for that account.
        Otherwise fetches for every known monetary account.
        """
        if not self._user_id:
            msg = "BunqConnector not authenticated"
            raise PermanentError(msg)

        if account_id:
            account_ids: Sequence[str] = [account_id]
        else:
            raw_accounts = await self.fetch_accounts()
            account_ids = [a.external_account_id for a in raw_accounts]

        all_txns: list[RawTransaction] = []
        for aid in account_ids:
            txns = await self._fetch_account_payments(aid, since, limit)
            all_txns.extend(txns)
            if limit and len(all_txns) >= limit:
                all_txns = all_txns[:limit]
                break

        return all_txns

    async def _fetch_account_payments(
        self,
        account_id: str,
        since: datetime,
        limit: int | None,
    ) -> list[RawTransaction]:
        """Fetch payments for a single monetary account with pagination.

        Filters out transactions older than *since* client-side to
        support bunq's server-side date-range limitations.
        """
        items: list[RawTransaction] = []
        url = f"/monetary-account/{account_id}/payment?count={_DEFAULT_COUNT}"

        while url:
            data = await self._request_paginated(url)
            for entry in data.get("Response", []):
                payment = entry.get("Payment")
                if payment is None:
                    continue
                txn = self._parse_payment(payment, account_id)
                if txn.occurred_at >= since:
                    items.append(txn)
                    if limit and len(items) >= limit:
                        return items
            url = self._next_page_url(data)

        return items

    @staticmethod
    def _parse_payment(
        data: dict[str, Any],
        account_id: str,
    ) -> RawTransaction:
        """Map a bunq Payment JSON object to a RawTransaction."""
        payment_id = str(data["id"])
        amount_data = data.get("amount", {})
        amount = Decimal(amount_data.get("value", "0"))
        currency = amount_data.get("currency", "EUR")

        created = _parse_bunq_datetime(data.get("created", ""))
        updated = _parse_bunq_datetime(data.get("updated", ""))

        description = data.get("description", "") or None
        payment_type = data.get("type", "")
        status_raw = data.get("status", "")

        counterparty = data.get("counterparty_alias") or {}
        counterparty_iban = counterparty.get("value", "")

        attachments = data.get("attachment", [])

        return RawTransaction(
            external_transaction_id=payment_id,
            external_account_id=account_id,
            amount=amount,
            currency_code=currency,
            occurred_at=created,
            booked_at=updated or created,
            description=description,
            transaction_type=_map_transaction_type(payment_type),
            status=_map_status(status_raw),
            provider_metadata={
                "payment_type": payment_type,
                "counterparty_iban": counterparty_iban,
                "attachment_count": len(attachments),
                "sub_type": data.get("sub_type"),
            },
        )

    # ── Pagination helper ───────────────────────────────────────────────

    async def _request_paginated(
        self,
        url: str,
    ) -> dict[str, Any]:
        """Make a paginated API request with auth headers.

        Handles rate-limit and auth-expired responses.
        """
        headers = self._auth_headers()

        try:
            response = await self._http.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
            raise  # unreachable — keeps type checker happy
        except httpx.TimeoutException as exc:
            msg = "bunq request timed out"
            raise TransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"bunq HTTP error: {exc}"
            raise TransientError(msg) from exc

    @staticmethod
    def _next_page_url(data: dict[str, Any]) -> str | None:
        """Extract the next-page URL from a paginated response.

        Returns ``None`` when there are no more pages.
        """
        pagination = data.get("Pagination") or data.get("PaginatedResponse")
        if not pagination:
            return None
        future_url = pagination.get("future_url")
        if future_url:
            # bunq returns future_url as an absolute path (/v1/...).
            # Strip leading /v1 since _BUNQ_API_BASE already includes it.
            future_url = future_url.removeprefix("/v1")
            return f"{_BUNQ_API_BASE}{future_url}"
        return None


# ── Module-level helpers ────────────────────────────────────────────────


def _base_headers() -> dict[str, str]:
    """Return headers common to all bunq API requests."""
    return {
        "X-Bunq-Client-Request-Id": _request_id(),
        "X-Bunq-Geolocation": "0 0 0 0 NL",
        "X-Bunq-Language": "en_US",
        "X-Bunq-Region": "NL",
        "Cache-Control": "no-cache",
    }


def _request_id() -> str:
    """Return a unique client-request-id per call."""
    import uuid

    return uuid.uuid4().hex[:16]


def _raise_for_status(response: httpx.Response) -> None:
    """Raise appropriate connector error from an HTTP error response."""
    status = response.status_code
    if status == 429:
        retry_after = _parse_retry_after(response)
        msg = "bunq rate limit exceeded"
        raise RateLimitError(msg, retry_after=retry_after)
    if status in (401, 403):
        msg = f"bunq authentication failed (HTTP {status})"
        raise PermanentError(msg)
    msg = f"bunq request failed (HTTP {status})"
    raise TransientError(msg)


def _parse_bunq_datetime(raw: str) -> datetime:
    """Parse a bunq timestamp to a UTC-aware datetime.

    Bunq formats::

        "2025-06-01 12:30:00.123456"
        "2025-06-01 12:30:00"
    """
    if not raw:
        return datetime.fromtimestamp(0, tz=UTC)

    if "." in raw:
        main, frac = raw.split(".", 1)
        frac = frac[:6]
        cleaned = f"{main}.{frac}"
    else:
        cleaned = raw

    # bunq returns naive UTC timestamps — parse and attach UTC
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(cleaned, fmt)  # noqa: DTZ007 — naive UTC returned by bunq, tz attached below
            break
        except ValueError:
            continue

    if parsed is None:
        return datetime.fromtimestamp(0, tz=UTC)

    return parsed.replace(tzinfo=UTC)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Extract ``Retry-After`` header value in seconds."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _map_transaction_type(bunq_type: str) -> str:
    """Map bunq payment type string to canonical transaction type."""
    mapping = {
        "BILLING": "payment",
        "PAYMENT": "payment",
        "TRANSFER": "transfer",
        "WITHDRAWAL": "withdrawal",
        "DEPOSIT": "deposit",
        "INTEREST": "interest",
        "FEE": "fee",
        "DIRECT_DEBIT": "payment",
        "SCT": "transfer",
        "SDD": "payment",
        "BUNQME": "payment",
        "REQUEST": "payment",
    }
    return mapping.get(bunq_type.upper(), "other")


def _map_status(raw: str) -> str:
    """Map bunq payment status to canonical status."""
    mapping = {
        "ACCEPTED": "booked",
        "PENDING": "pending",
        "REJECTED": "cancelled",
        "CANCELLED": "cancelled",
        "REVERSED": "reversed",
    }
    return mapping.get(raw.upper(), "pending")
