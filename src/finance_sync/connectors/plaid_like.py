"""Plaid-like Open Banking connector.

Simulates and connects to Plaid / TrueLayer / Teller-style open banking
APIs.  Uses token-based auth (public_token → access_token exchange) and
cursor-based pagination for transactions.

This connector provides a template for open banking integrations with
a working mock implementation.  Replace the mock HTTP calls with real
``httpx`` requests for production use.

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
from typing import TYPE_CHECKING, Any

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import RawAccount, RawTransaction
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
        This implementation includes mock data for sandbox mode.
        For production, replace the mock responses with real API calls.
    """

    display_name = "Plaid-like Open Banking"
    sdk_version = "0.1.0"

    rate_limit_policy = RateLimitPolicy(
        max_requests=100,
        window_seconds=60,
        max_retries=3,
        backoff_base=1.0,
    )

    def __init__(
        self,
        config: ConnectorConfig,
    ) -> None:
        super().__init__(config)
        self._environment: str = config.options.get("environment", "production")

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

        # For sandbox, accept any token
        if self._environment == "sandbox":
            self._authenticated = True
            return

        if not client_id or not access_token:
            msg = (
                "Plaid-like connector needs client_id and "
                "access_token credentials"
            )
            raise PermanentError(msg)

        # TODO: POST /item/get to validate token in production
        self._authenticated = True

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
        _max = limit or 100
        _page_size = min(_max, 500)

        if self._environment != "sandbox":
            # Real implementation would make API call here
            return []

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

        return [
            RawTransaction(
                external_transaction_id=t["transaction_id"],
                external_account_id=t["account_id"],
                amount=Decimal(str(t["amount"])),
                currency_code=t["iso_currency_code"],
                occurred_at=datetime.fromisoformat(t["date"]).replace(
                    tzinfo=UTC
                ),
                description=(t.get("merchant_name") or t.get("name", "")),
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

    # ── Transform overrides ────────────────────────────────────────────

    def transform_accounts(
        self,
        raw: list[RawAccount],
    ) -> list[RawAccount]:
        """Normalise Plaid account types to finance-sync canonical types.

        Plaid uses 'depository' — we map it to 'checking' or 'savings'
        based on subtype.
        """
        result = []
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
                RawAccount(
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
