"""Plaid-like Open Banking connector.

Simulates and connects to Plaid / TrueLayer / Teller-style open banking
APIs. Uses token-based auth and bounded date-range pagination for transactions.

Sandbox mode keeps deterministic fixtures for local development. Production
mode uses the Plaid HTTP API through an injectable ``httpx`` client.

Credentials
    ``config.credentials["client_id"]`` — Plaid-style client ID.
    ``config.credentials["access_token"]`` — Plaid-style access token.
    ``config.options["environment"]`` — ``"sandbox"``, ``"development"``,
    or ``"production"`` (default: ``"production"``).
    ``config.options["country_codes"]`` — List of country codes
    (default: ``["NL", "US"]``).

Rate limit
    Open banking APIs typically allow 100 requests per minute.
    The connector enforces this globally.

Example::

    config = ConnectorConfig(
        provider_type="plaid_like",
        credentials={
            "client_id": "plaid_client_123",
            "access_token": "access-sandbox-abc",
        },
        options={"environment": "sandbox", "country_codes": ["NL", "US"]},
    )
    conn = PlaidLikeConnector(config)
    await conn.authenticate()
    accounts = await conn.fetch_accounts()
    txns = await conn.fetch_transactions(since=...)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NoReturn, cast

import httpx

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import (
    CanonicalAccountData,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.rate_limiter import RateLimitPolicy

if TYPE_CHECKING:
    from finance_sync.connectors.models import ConnectorConfig

# ── Account type normalisation ───────────────────────────────────────────

_PLAID_TYPE_MAP: dict[str, str] = {
    "depository": "checking",
    "credit": "credit",
    "loan": "loan",
    "investment": "investment",
    "brokerage": "brokerage",
    "other": "other",
}

_PLAID_API_BASES = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}
_PLAID_TIMEOUT = 30.0


def _required_string(value: object, field: str) -> str:
    """Validate a required string in a provider payload."""
    if not isinstance(value, str):
        message = f"Plaid payload field {field!r} must be a string"
        raise ValueError(message)
    return value


def _plaid_to_canonical_type(plaid_type: str, subtype: str) -> str:
    """Map Plaid account type + subtype to canonical type.

    Plaid uses 'depository' for both checking and savings.
    We differentiate using the subtype.
    """
    if plaid_type == "depository":
        if subtype and subtype.lower() == "savings":
            return "savings"
        return "checking"
    return _PLAID_TYPE_MAP.get(plaid_type, "other")


def _raise_for_status(response: httpx.Response) -> NoReturn:
    """Translate Plaid HTTP failures without persisting response bodies."""
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            parsed_retry_after = float(retry_after) if retry_after else None
        except (TypeError, ValueError):
            parsed_retry_after = None
        message = "Plaid-like rate limit exceeded"
        raise RateLimitError(message, retry_after=parsed_retry_after)
    if response.status_code in (401, 403):
        message = "Plaid-like authentication failed"
        raise PermanentError(message)
    if response.status_code in (400, 404):
        message = "Plaid-like request was rejected"
        raise PermanentError(message)
    message = "Plaid-like provider request failed"
    raise TransientError(message)


class PlaidLikeConnector(Connector):
    """Connector for Plaid / TrueLayer / Teller open banking APIs.

    Key features:

    * Token-based credential flow (public_token → access_token exchange)
    * Item / access-model (one access token = one institution link)
    * Cursor-based transaction pagination
    * Account type normalisation (depository, credit, loan, investment)
    * Transaction enrichment (merchant, category from provider metadata)
    * Sandbox / development / production environment switching

    Note:
        Sandbox mode uses deterministic fixtures; production mode calls the
        bounded Plaid transaction endpoint.
    """

    display_name = "Plaid-like Open Banking"
    sdk_version = "0.1.0"
    remediation_strategies = {
        "transaction_history_gap": {
            "endpoint_family": "transaction_history",
            "batch_limit": 1,
        }
    }

    rate_limit_policy = RateLimitPolicy(
        max_requests=100,
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
        super().__init__(config)
        self._environment: str = config.options.get("environment", "production")
        base_url = config.options.get(
            "base_url", _PLAID_API_BASES.get(self._environment)
        )
        if not isinstance(base_url, str) or not base_url:
            message = "Plaid-like environment has no API base URL"
            raise PermanentError(message)
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(_PLAID_TIMEOUT),
        )

    @property
    def name(self) -> str:
        return "plaid_like"

    # ── Auth ───────────────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Exchange public_token for access_token, or validate existing one.

        In a real implementation this would:
        1. POST /item/public_token/exchange with public_token → access_token
        2. Or POST /item/get to validate an existing access_token
        """
        client_id = self.config.credentials.get("client_id")
        access_token = self.config.credentials.get("access_token")
        secret = self.config.credentials.get("secret")

        # For sandbox, accept any token
        if self._environment == "sandbox":
            self._authenticated = True
            return

        if not client_id or not access_token or not secret:
            msg = (
                "Plaid-like connector needs client_id and "
                "secret and access_token credentials"
            )
            raise PermanentError(msg)

        try:
            response = await self._http.post(
                "/item/get",
                json=self._auth_payload(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc.response)
        except httpx.TimeoutException as exc:
            message = "Plaid-like authentication timed out"
            raise TransientError(message) from exc
        except httpx.HTTPError as exc:
            message = "Plaid-like authentication request failed"
            raise TransientError(message) from exc
        self._authenticated = True

    def _auth_payload(self) -> dict[str, str]:
        """Return the minimal Plaid request credentials."""
        return {
            "client_id": self.config.credentials["client_id"],
            "secret": self.config.credentials["secret"],
            "access_token": self.config.credentials["access_token"],
        }

    # ── Accounts ───────────────────────────────────────────────────────

    async def fetch_accounts(self) -> list[RawAccount]:
        """Fetch accounts via GET /accounts/get.

        Plaid response shape (simplified)::

            {
                "accounts": [{
                    "account_id": "BxXx...",
                    "name": "Plaid Checking",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "current": 110.12,
                        "available": 100.12,
                        "iso_currency_code": "USD"
                    }
                }]
            }
        """
        # Mock data for sandbox — replace with real API call
        _fake_accounts: list[dict[str, Any]] = []
        if self._environment == "sandbox":
            _fake_accounts = [
                {
                    "account_id": "plaid_acc_checking_01",
                    "name": "Plaid Checking",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "current": 1250.50,
                        "available": 1200.00,
                        "iso_currency_code": "EUR",
                    },
                },
                {
                    "account_id": "plaid_acc_savings_01",
                    "name": "Plaid Savings",
                    "type": "depository",
                    "subtype": "savings",
                    "balances": {
                        "current": 15000.00,
                        "available": 15000.00,
                        "iso_currency_code": "EUR",
                    },
                },
                {
                    "account_id": "plaid_acc_credit_01",
                    "name": "Plaid Credit Card",
                    "type": "credit",
                    "subtype": "credit card",
                    "balances": {
                        "current": -450.25,
                        "available": 550.00,
                        "iso_currency_code": "EUR",
                    },
                },
            ]

        if not _fake_accounts:
            return []

        return [
            RawAccount(
                external_account_id=a["account_id"],
                name=a["name"],
                account_type=_plaid_to_canonical_type(a["type"], a["subtype"]),
                account_subtype=a["subtype"],
                currency_code=a["balances"]["iso_currency_code"],
                current_balance=Decimal(str(a["balances"]["current"])),
                available_balance=Decimal(str(a["balances"]["available"])),
                iso_currency_code=a["balances"]["iso_currency_code"],
                provider_metadata={
                    "environment": self._environment,
                },
            )
            for a in _fake_accounts
        ]

    # ── Transactions with cursor-based pagination ──────────────────────

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Fetch transactions with cursor-based pagination.

        Plaid uses ``/transactions/sync`` with a cursor::

            POST /transactions/sync
            {
                "access_token": "...",
                "cursor": "...",
                "count": 100
            }

        Returns::

            {
                "added": [...],
                "modified": [...],
                "removed": [...],
                "next_cursor": "...",
                "has_more": true
            }
        """
        _max = min(max(limit or 100, 1), 500)
        _page_size = _max

        if self._environment != "sandbox":
            return await self._fetch_production_transactions(
                since, account_id=account_id, limit=_max
            )

        # Mock data for sandbox
        _fake_txns = [
            {
                "transaction_id": (f"plaid_tx_{account_id or 'checking'}_001"),
                "account_id": account_id or "plaid_acc_checking_01",
                "amount": -75.50,
                "iso_currency_code": "EUR",
                "date": since.strftime("%Y-%m-%d"),
                "name": "Supermarket Inc.",
                "merchant_name": "Albert Heijn",
                "category": ["Food and Drink", "Groceries"],
                "pending": False,
            },
            {
                "transaction_id": (f"plaid_tx_{account_id or 'checking'}_002"),
                "account_id": account_id or "plaid_acc_checking_01",
                "amount": -12.99,
                "iso_currency_code": "EUR",
                "date": since.strftime("%Y-%m-%d"),
                "name": "Streaming Service",
                "merchant_name": "Netflix",
                "category": ["Entertainment"],
                "pending": False,
            },
        ]

        def transaction_description(value: dict[str, Any]) -> str | None:
            merchant_name = value.get("merchant_name")
            if isinstance(merchant_name, str):
                return merchant_name
            name = value.get("name")
            return name if isinstance(name, str) else None

        return [
            RawTransaction(
                external_transaction_id=_required_string(
                    t["transaction_id"], "transaction_id"
                ),
                external_account_id=_required_string(
                    t["account_id"], "account_id"
                ),
                amount=Decimal(str(t["amount"])),
                currency_code=_required_string(
                    t["iso_currency_code"], "iso_currency_code"
                ),
                occurred_at=datetime.fromisoformat(
                    _required_string(t["date"], "date")
                ).replace(tzinfo=UTC),
                description=transaction_description(t),
                transaction_type="payment",
                status="pending" if t.get("pending") else "booked",
                provider_metadata={
                    "merchant_name": t.get("merchant_name"),
                    "category": t.get("category", []),
                    "environment": self._environment,
                },
            )
            for t in _fake_txns[:_page_size]
        ]

    async def _fetch_production_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None,
        limit: int,
    ) -> list[RawTransaction]:
        """Fetch a bounded date range from Plaid's transaction endpoint."""
        start = (
            since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        ).date()
        end = datetime.now(UTC).date()
        if start > end:
            return []
        transactions: list[dict[str, Any]] = []
        offset = 0
        while len(transactions) < limit:
            page_size = min(500, limit - len(transactions))
            payload: dict[str, Any] = {
                **self._auth_payload(),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "count": page_size,
                "offset": offset,
            }
            if account_id:
                payload["account_id"] = account_id
            try:
                response = await self._http.post(
                    "/transactions/get", json=payload
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc.response)
            except httpx.TimeoutException as exc:
                message = "Plaid-like transaction fetch timed out"
                raise TransientError(message) from exc
            except httpx.HTTPError as exc:
                message = "Plaid-like transaction fetch request failed"
                raise TransientError(message) from exc
            body = response.json()
            page_value = body.get("transactions", [])
            if not isinstance(page_value, list):
                message = "Plaid-like transaction response is malformed"
                raise PermanentError(message)
            page = cast("list[Any]", page_value)
            transactions.extend(
                cast("dict[str, Any]", value)
                for value in page
                if isinstance(value, dict)
            )
            if len(page) < page_size:
                break
            offset += len(page)
        return [self._raw_transaction(value) for value in transactions[:limit]]

    @staticmethod
    def _raw_transaction(value: dict[str, Any]) -> RawTransaction:
        transaction_id = _required_string(
            value.get("transaction_id"), "transaction_id"
        )
        account_id = _required_string(value.get("account_id"), "account_id")
        currency = _required_string(
            value.get("iso_currency_code") or "EUR", "iso_currency_code"
        )
        date_value = _required_string(value.get("date"), "date")
        occurred_at = datetime.fromisoformat(date_value).replace(tzinfo=UTC)
        merchant_name = value.get("merchant_name")
        raw_name = value.get("name")
        name = (
            merchant_name
            if isinstance(merchant_name, str)
            else raw_name
            if isinstance(raw_name, str)
            else None
        )
        return RawTransaction(
            external_transaction_id=transaction_id,
            external_account_id=account_id,
            amount=Decimal(str(value.get("amount", 0))),
            currency_code=currency,
            occurred_at=occurred_at,
            description=name if isinstance(name, str) else None,
            transaction_type="payment",
            status="pending" if value.get("pending") else "booked",
            provider_metadata={
                "merchant_name": value.get("merchant_name"),
                "category": value.get("category", []),
                "environment": "production",
            },
        )

    # ── Transform overrides ────────────────────────────────────────────

    def transform_accounts(
        self,
        raw: list[RawAccount],
    ) -> list[CanonicalAccountData]:
        """Normalise Plaid account types to finance-sync canonical types.

        Plaid uses 'depository' — we map it to 'checking' or 'savings'
        based on subtype.
        """
        result: list[CanonicalAccountData] = []
        for r in raw:
            acct_type = r.account_type
            if acct_type == "depository":
                if r.account_subtype == "savings":
                    acct_type = "savings"
                else:
                    acct_type = "checking"
            elif acct_type == "credit":
                acct_type = "credit"

            result.append(
                CanonicalAccountData(
                    provider_key=self.name,
                    external_account_id=r.external_account_id,
                    name=r.name,
                    account_type=acct_type,
                    account_subtype=r.account_subtype,
                    currency_code=r.currency_code,
                    current_balance=r.current_balance,
                    available_balance=r.available_balance,
                    iso_currency_code=r.iso_currency_code,
                    provider_metadata=r.provider_metadata,
                )
            )
        return result
