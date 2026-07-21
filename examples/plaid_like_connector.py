"""
Plaid-like Connector Example
============================

Demonstrates a connector plugin that mimics Plaid's transaction and account
API pattern (token-based auth, item/access model, cursor-based pagination).

This is a **template** — it shows the structure but uses fake data.
Replace the mock HTTP calls with real ``httpx``/``aiohttp`` requests.

Usage::

    pip install finance-sync-sdk
    # Register in pyproject.toml:
    # [project.entry-points."finance_sync_sdk.plugins"]
    # plaid_like = "examples.plaid_like_connector:PlaidLikeConnector"

    from finance_sync_sdk import ConnectorConfig, PluginRegistry

    registry = PluginRegistry()
    config = ConnectorConfig(
        provider_type="plaid_like",
        credentials={"client_id": "...", "access_token": "..."},
        options={"environment": "sandbox", "country_codes": ["NL", "US"]},
    )
    connector = registry.get_connector(config)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from finance_sync_sdk import ConnectorPlugin
from finance_sync_sdk.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync_sdk.rate_limiter import RateLimitPolicy


class PlaidLikeConnector(ConnectorPlugin):
    """Template connector simulating a Plaid / TrueLayer / Teller API.

    Key concepts demonstrated:

    * Token-based credential flow (public_token → access_token exchange)
    * Item / access-model (one access token = one institution link)
    * Cursor-based transaction pagination
    * Account type normalisation (depository, credit, loan, investment)
    * Transaction enrichment (Merchant, Category from provider metadata)
    * Sandbox / production environment switching
    """

    display_name = "Plaid-like Open Banking"
    plugin_version = "0.1.0"

    rate_limit_policy = RateLimitPolicy(
        max_requests=100,
        window_seconds=60,
        max_retries=3,
        backoff_base=1.0,
        metadata={"provider_policy_url": "https://plaid.com/docs/api/rate-limits/"},
    )

    @property
    def name(self) -> str:
        return "plaid_like"

    # ── Auth ───────────────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Exchange a public_token for an access_token, or validate an existing one.

        In a real implementation this would:
        1. POST /item/public_token/exchange with public_token → get access_token
        2. Or POST /item/get to validate an existing access_token
        """
        client_id = self.config.credentials.get("client_id")
        access_token = self.config.credentials.get("access_token")

        # For sandbox, accept hard-coded tokens
        if self.config.options.get("environment") == "sandbox":
            self._authenticated = True
            return

        if not client_id or not access_token:
            from finance_sync_sdk.exceptions import PermanentError

            raise PermanentError(
                "Plaid connector requires client_id and access_token credentials"
            )

        # TODO: POST /item/get to validate token
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
        environment = self.config.options.get("environment", "production")

        # Mock data for demonstration — replace with real API call
        _fake_accounts = [
            {
                "account_id": "plaid_acc_checking_01",
                "name": "Plaid Checking",
                "type": "depository",
                "subtype": "checking",
                "balances": {"current": 1250.50, "available": 1200.00, "iso_currency_code": "EUR"},
            },
            {
                "account_id": "plaid_acc_savings_01",
                "name": "Plaid Savings",
                "type": "depository",
                "subtype": "savings",
                "balances": {"current": 15000.00, "available": 15000.00, "iso_currency_code": "EUR"},
            },
            {
                "account_id": "plaid_acc_credit_01",
                "name": "Plaid Credit Card",
                "type": "credit",
                "subtype": "credit card",
                "balances": {"current": -450.25, "available": 550.00, "iso_currency_code": "EUR"},
            },
        ]

        return [
            RawAccount(
                external_account_id=a["account_id"],
                name=a["name"],
                account_type=a["type"],
                account_subtype=a["subtype"],
                currency_code=a["balances"]["iso_currency_code"],
                current_balance=Decimal(str(a["balances"]["current"])),
                available_balance=Decimal(str(a["balances"]["available"])),
                iso_currency_code=a["balances"]["iso_currency_code"],
                provider_metadata={
                    "environment": environment,
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
                "cursor": "..."  # from the previous response
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

        # In a real implementation you'd loop cursor pages
        cursor = self.config.options.get("_cursor")
        environment = self.config.options.get("environment", "production")

        # Mock data — replace with real paginated API call
        _fake_txns = [
            {
                "transaction_id": f"plaid_tx_{account_id or 'checking'}_001",
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
                "transaction_id": f"plaid_tx_{account_id or 'checking'}_002",
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
                occurred_at=datetime.fromisoformat(t["date"]).replace(tzinfo=UTC),
                description=t.get("merchant_name") or t.get("name", ""),
                transaction_type="payment",
                status="pending" if t.get("pending") else "booked",
                provider_metadata={
                    "merchant_name": t.get("merchant_name"),
                    "category": t.get("category", []),
                    "environment": environment,
                },
            )
            for t in _fake_txns[: _page_size]
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
