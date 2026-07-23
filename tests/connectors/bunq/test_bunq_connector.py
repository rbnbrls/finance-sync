"""Contract tests + unit tests for the Bunq connector.

These tests use a mock HTTP transport to simulate the bunq API
without any network calls.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from finance_sync.connectors.bunq import (
    _map_status,
    _map_transaction_type,
    _parse_bunq_datetime,
)
from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)

if TYPE_CHECKING:
    from finance_sync.connectors.bunq import BunqConnector
    from tests.connectors.bunq.conftest import BunqApiMockTransport

# Module-level asyncio is NOT set — each test class declares its own
# marker so sync tests don't get accidental asyncio treatment.

# ═══════════════════════════════════════════════════════════════════════
# Contract tests (from ConnectorContractTest template)
# ═══════════════════════════════════════════════════════════════════════


class TestBunqConnectorContract:
    """Contract tests that every connector must pass.

    See :class:`tests.connectors.contract_test_template.ConnectorContractTest`.
    """

    pytestmark = pytest.mark.asyncio

    # ── Authentication ────────────────────────────────────────────────

    async def test_authenticate_success(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Connector should authenticate without raising."""
        await bunq_connector.authenticate()

    async def test_authenticate_idempotent(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Calling authenticate twice should be safe."""
        await bunq_connector.authenticate()
        await bunq_connector.authenticate()

    async def test_authenticate_missing_api_key(self) -> None:
        """Missing api_key should raise PermanentError."""
        config = ConnectorConfig(provider_type="bunq")
        from finance_sync.connectors.bunq import BunqConnector

        conn = BunqConnector(config)
        with pytest.raises(PermanentError, match="api_key"):
            await conn.authenticate()

    # ── Health ─────────────────────────────────────────────────────────

    async def test_health_returns_health(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Health check should return a ConnectorHealth object."""
        health = await bunq_connector.health()
        assert health.provider_type == bunq_connector.name
        assert health.healthy

    # ── Accounts ───────────────────────────────────────────────────────

    async def test_fetch_accounts_returns_list(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_accounts should return a list of RawAccount."""
        await bunq_connector.authenticate()
        accounts = await bunq_connector.fetch_accounts()
        assert isinstance(accounts, list)
        assert len(accounts) == 3  # bank + savings + savings goal

        # First account should be the checking account
        checking = accounts[0]
        assert isinstance(checking, RawAccount)
        assert checking.external_account_id == "1000001"
        assert checking.name == "Main Checking"
        assert checking.account_type == "checking"

        # Second should be savings
        savings = accounts[1]
        assert savings.external_account_id == "1000002"
        assert savings.account_type == "savings"

        # Third should be savings goal
        goal = accounts[2]
        assert goal.external_account_id == "1000003"
        assert goal.account_type == "savings"

    async def test_fetch_accounts_idempotent(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Calling fetch_accounts twice should be safe."""
        await bunq_connector.authenticate()
        first = await bunq_connector.fetch_accounts()
        second = await bunq_connector.fetch_accounts()
        assert isinstance(first, list)
        assert isinstance(second, list)
        assert len(first) == len(second)

    async def test_fetch_accounts_not_authenticated(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Calling fetch_accounts before authenticate should raise."""
        with pytest.raises(PermanentError, match="not authenticated"):
            await bunq_connector.fetch_accounts()

    # ── Transactions ───────────────────────────────────────────────────

    async def test_fetch_transactions_returns_list(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_transactions should return a list of RawTransaction."""
        await bunq_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await bunq_connector.fetch_transactions(since=since)
        assert isinstance(txns, list)
        assert len(txns) >= 1

        # Verify first transaction
        txn = txns[0]
        assert isinstance(txn, RawTransaction)
        assert txn.external_transaction_id
        assert txn.external_account_id
        assert txn.amount is not None

    async def test_fetch_transactions_with_account_filter(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_transactions should accept an account_id filter."""
        await bunq_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await bunq_connector.fetch_transactions(
            since=since, account_id="1000001"
        )
        assert isinstance(txns, list)
        # Account 1000001 has 3 payments in fixtures
        assert len(txns) == 3

    async def test_fetch_transactions_with_limit(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_transactions should accept a limit parameter."""
        await bunq_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await bunq_connector.fetch_transactions(since=since, limit=2)
        assert isinstance(txns, list)
        assert len(txns) <= 2

    async def test_fetch_transactions_not_authenticated(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Calling fetch_transactions before authenticate should raise."""
        since = datetime(2025, 1, 1, tzinfo=UTC)
        with pytest.raises(PermanentError, match="not authenticated"):
            await bunq_connector.fetch_transactions(since=since)

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_accounts_roundtrip(
        self,
        bunq_connector: BunqConnector,
        sample_bunq_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should map RawAccount to CanonicalAccountData."""
        raw_accounts, _ = sample_bunq_raw_data
        assert len(raw_accounts) > 0  # sanity

        canonical = bunq_connector.transform_accounts(raw_accounts)
        assert len(canonical) == len(raw_accounts)
        for ca in canonical:
            assert isinstance(ca, CanonicalAccountData)
            assert ca.provider_key == "bunq"
            assert ca.external_account_id
            assert ca.account_type

        # Verify specific mappings
        checking = canonical[0]
        assert checking.name == "Main Checking"
        assert checking.account_type == "checking"
        assert checking.current_balance == Decimal("1520.45")

        savings = canonical[1]
        assert savings.name == "Emergency Fund"
        assert savings.account_type == "savings"

    async def test_transform_transactions_roundtrip(
        self,
        bunq_connector: BunqConnector,
        sample_bunq_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should map RawTransaction to CanonicalTransactionData."""
        _, raw_txns = sample_bunq_raw_data
        assert len(raw_txns) > 0

        canonical = bunq_connector.transform_transactions(raw_txns)
        assert len(canonical) == len(raw_txns)
        for ct in canonical:
            assert isinstance(ct, CanonicalTransactionData)
            assert ct.provider_key == "bunq"
            assert ct.external_transaction_id
            assert ct.transaction_type
            assert ct.status

        purchase = canonical[0]
        assert purchase.amount == Decimal("-42.50")
        assert purchase.transaction_type == "payment"
        assert purchase.status == "booked"
        assert purchase.description == "Coffee shop"

    # ── Name ───────────────────────────────────────────────────────────

    async def test_name_is_string(self, bunq_connector: BunqConnector) -> None:
        """The name property should return a non-empty string."""
        assert isinstance(bunq_connector.name, str)
        assert len(bunq_connector.name) > 0
        assert bunq_connector.name == "bunq"
        assert bunq_connector.name == bunq_connector.config.provider_type

    async def test_display_name(self, bunq_connector: BunqConnector) -> None:
        """display_name should be set."""
        assert bunq_connector.display_name == "Bunq"

    async def test_sdk_version(self, bunq_connector: BunqConnector) -> None:
        """sdk_version should be a valid string."""
        assert bunq_connector.sdk_version == "0.1.0"


# ═══════════════════════════════════════════════════════════════════════
# Unit tests — Bunq-specific logic
# ═══════════════════════════════════════════════════════════════════════


class TestBunqConnectorAuth:
    """Authentication-specific behaviour."""

    pytestmark = pytest.mark.asyncio

    async def test_authenticate_sets_session_and_user_id(
        self,
        bunq_connector: BunqConnector,
        bunq_mock_transport: BunqApiMockTransport,
    ) -> None:
        """After auth, connector should have session token and user ID."""
        assert bunq_connector._session_token is None
        assert bunq_connector._user_id is None

        await bunq_connector.authenticate()

        assert (
            bunq_connector._session_token
            == "bunq_session_token_test_abcdef123456"
        )
        assert bunq_connector._user_id == 54321
        # Should have made exactly 1 POST to session-server
        session_calls = [
            c
            for c in bunq_mock_transport.call_log
            if "session-server" in str(c["url"])
        ]
        assert len(session_calls) == 1

    async def test_auth_headers_after_authenticate(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Auth headers should include session token and standard headers."""
        await bunq_connector.authenticate()
        headers = bunq_connector._auth_headers()
        assert "X-Bunq-Client-Authentication" in headers
        assert headers["X-Bunq-Client-Authentication"] == (
            "bunq_session_token_test_abcdef123456"
        )
        assert headers["X-Bunq-Language"] == "en_US"

    async def test_auth_headers_before_authenticate_raises(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Accessing auth headers before authenticate should raise."""
        with pytest.raises(PermanentError, match="not authenticated"):
            bunq_connector._auth_headers()


class TestBunqConnectorPagination:
    """Pagination for account fetching (async, uses mock transport)."""

    pytestmark = pytest.mark.asyncio

    async def test_paginated_accounts_fetch_all_pages(
        self,
        bunq_connector_config: ConnectorConfig,
        bunq_mock_transport: BunqApiMockTransport,
    ) -> None:
        """Connector should follow pagination links for accounts."""
        import httpx

        from finance_sync.connectors.bunq import BunqConnector

        http_client = httpx.AsyncClient(
            base_url="https://api.bunq.com/v1",
            transport=bunq_mock_transport,
        )
        conn = BunqConnector(
            config=bunq_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        accounts = await conn.fetch_accounts()

        # Should have called monetary-account twice (page 1 + page 2)
        account_calls = [
            c
            for c in bunq_mock_transport.call_log
            if "monetary-account" in str(c["url"])
            and "payment" not in str(c["url"])
        ]
        assert len(account_calls) >= 2
        # Should have accounts from both pages
        assert len(accounts) == 3


class TestBunqConnectorPaginationHelpers:
    """Pagination helper methods (sync, no mock transport needed)."""

    def test_next_page_url_with_future(self) -> None:
        """_next_page_url should extract the future_url."""
        from finance_sync.connectors.bunq import BunqConnector

        data = {
            "Pagination": {
                "future_url": "/v1/user/54321/monetary-account?newer_id=1000001"
            }
        }
        url = BunqConnector._next_page_url(data)
        assert url is not None
        assert "newer_id=1000001" in url

    def test_next_page_url_none(self) -> None:
        """_next_page_url should return None when there are no more pages."""
        from finance_sync.connectors.bunq import BunqConnector

        data = {"Pagination": {"future_url": None}}
        url = BunqConnector._next_page_url(data)
        assert url is None

    def test_next_page_url_missing_pagination(self) -> None:
        """_next_page_url should return None when Pagination key is missing."""
        from finance_sync.connectors.bunq import BunqConnector

        data: dict = {}
        url = BunqConnector._next_page_url(data)
        assert url is None


class TestBunqTransactionMapping:
    """Transaction type and status mapping."""

    def test_map_payment_types(self) -> None:
        """Bunq payment types should map to canonical types correctly."""
        assert _map_transaction_type("PAYMENT") == "payment"
        assert _map_transaction_type("TRANSFER") == "transfer"
        assert _map_transaction_type("INTEREST") == "interest"
        assert _map_transaction_type("FEE") == "fee"
        assert _map_transaction_type("WITHDRAWAL") == "withdrawal"
        assert _map_transaction_type("DEPOSIT") == "deposit"
        assert _map_transaction_type("BILLING") == "payment"
        assert _map_transaction_type("DIRECT_DEBIT") == "payment"
        assert _map_transaction_type("SCT") == "transfer"
        assert _map_transaction_type("SDD") == "payment"
        assert _map_transaction_type("BUNQME") == "payment"
        assert _map_transaction_type("REQUEST") == "payment"
        assert _map_transaction_type("UNKNOWN_TYPE") == "other"
        assert _map_transaction_type("") == "other"

    def test_map_payment_types_case_insensitive(self) -> None:
        """Mapping should be case-insensitive."""
        assert _map_transaction_type("payment") == "payment"
        assert _map_transaction_type("Transfer") == "transfer"

    def test_map_status(self) -> None:
        """Bunq statuses should map to canonical statuses correctly."""
        assert _map_status("ACCEPTED") == "booked"
        assert _map_status("PENDING") == "pending"
        assert _map_status("REJECTED") == "cancelled"
        assert _map_status("CANCELLED") == "cancelled"
        assert _map_status("REVERSED") == "reversed"
        assert _map_status("UNKNOWN") == "pending"
        assert _map_status("") == "pending"

    def test_map_status_case_insensitive(self) -> None:
        """Status mapping should be case-insensitive."""
        assert _map_status("accepted") == "booked"
        assert _map_status("Pending") == "pending"


class TestBunqDatetimeParsing:
    """Bunq-specific datetime parsing."""

    def test_parse_full_datetime(self) -> None:
        """Parse a bunq datetime with microseconds."""
        dt = _parse_bunq_datetime("2025-06-15 14:30:00.123456")
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.day == 15
        assert dt.hour == 14
        assert dt.minute == 30
        assert dt.second == 0
        assert dt.microsecond == 123456
        assert dt.tzinfo is not None

    def test_parse_datetime_no_microseconds(self) -> None:
        """Parse a bunq datetime without microseconds."""
        dt = _parse_bunq_datetime("2025-06-01 09:00:00")
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.day == 1
        assert dt.hour == 9
        assert dt.microsecond == 0
        assert dt.tzinfo is not None

    def test_parse_empty_string(self) -> None:
        """Empty string should return epoch."""
        dt = _parse_bunq_datetime("")
        assert dt.tzinfo is not None
        assert dt.replace(tzinfo=UTC).timestamp() == 0.0


class TestBunqConnectorErrorHandling:
    """Error classification and handling."""

    pytestmark = pytest.mark.asyncio

    async def test_rate_limit_error(
        self,
        bunq_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 429 should raise RateLimitError."""
        import httpx

        from finance_sync.connectors.bunq import BunqConnector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(429, json={"error": "Too Many Requests"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://api.bunq.com/v1", transport=transport
        )
        conn = BunqConnector(
            config=bunq_connector_config, http_client=http_client
        )

        with pytest.raises(RateLimitError):
            await conn.authenticate()

    async def test_authentication_error(
        self,
        bunq_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 401 should raise PermanentError."""
        import httpx

        from finance_sync.connectors.bunq import BunqConnector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(401, json={"error": "Unauthorized"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://api.bunq.com/v1", transport=transport
        )
        conn = BunqConnector(
            config=bunq_connector_config, http_client=http_client
        )

        with pytest.raises(PermanentError, match="authentication failed"):
            await conn.authenticate()

    async def test_forbidden_error(
        self,
        bunq_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 403 should raise PermanentError."""
        import httpx

        from finance_sync.connectors.bunq import BunqConnector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(403, json={"error": "Forbidden"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://api.bunq.com/v1", transport=transport
        )
        conn = BunqConnector(
            config=bunq_connector_config, http_client=http_client
        )

        with pytest.raises(PermanentError, match="authentication failed"):
            await conn.authenticate()

    async def test_server_error(
        self,
        bunq_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 500 should raise TransientError."""
        import httpx

        from finance_sync.connectors.bunq import BunqConnector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(500, json={"error": "Internal Server Error"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://api.bunq.com/v1", transport=transport
        )
        conn = BunqConnector(
            config=bunq_connector_config, http_client=http_client
        )

        with pytest.raises(TransientError):
            await conn.authenticate()

    async def test_transient_error_on_fetch(
        self,
        bunq_connector: BunqConnector,
        bunq_mock_transport: BunqApiMockTransport,
    ) -> None:
        """After auth succeeds, request errors classified correctly."""
        await bunq_connector.authenticate()

        # We can't easily inject errors mid-session with the mock transport,
        # but we can verify that the error classification method works
        import httpx

        from finance_sync.connectors.bunq import _raise_for_status

        with pytest.raises(RateLimitError):
            resp = httpx.Response(429)
            _raise_for_status(resp)

        with pytest.raises(PermanentError):
            resp = httpx.Response(401)
            _raise_for_status(resp)

        with pytest.raises(PermanentError):
            resp = httpx.Response(403)
            _raise_for_status(resp)

        with pytest.raises(TransientError):
            resp = httpx.Response(503)
            _raise_for_status(resp)
