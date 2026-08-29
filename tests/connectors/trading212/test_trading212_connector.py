"""Contract tests + unit tests for the Trading212 connector.

These tests use a mock HTTP transport to simulate the Trading212 API
without any network calls.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    RawTransaction,
)
from finance_sync.connectors.trading212 import (
    _map_order_side,
    _map_order_status,
    _map_transaction_type,
    _parse_cash_transaction,
    _parse_order,
    _parse_t212_datetime,
)

if TYPE_CHECKING:
    from finance_sync.connectors.trading212 import Trading212Connector

# ═══════════════════════════════════════════════════════════════════════
# Contract tests (from ConnectorContractTest template)
# ═══════════════════════════════════════════════════════════════════════


class TestTrading212ConnectorContract:
    """Contract tests that every connector must pass."""

    pytestmark = pytest.mark.asyncio

    # ── Authentication ────────────────────────────────────────────────

    async def test_authenticate_success(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Connector should authenticate without raising."""
        await t212_connector.authenticate()

    async def test_authenticate_idempotent(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Calling authenticate twice should be safe."""
        await t212_connector.authenticate()
        await t212_connector.authenticate()

    async def test_authenticate_missing_api_key(self) -> None:
        """Missing api_key should raise PermanentError."""
        config = ConnectorConfig(provider_type="trading212")
        from finance_sync.connectors.trading212 import Trading212Connector

        conn = Trading212Connector(config)
        with pytest.raises(PermanentError, match="api_key"):
            await conn.authenticate()

    # ── Health ─────────────────────────────────────────────────────────

    async def test_health_returns_health(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Health check should return a ConnectorHealth object."""
        health = await t212_connector.health()
        assert health.provider_type == t212_connector.name
        assert health.healthy

    # ── Accounts ───────────────────────────────────────────────────────

    async def test_fetch_accounts_returns_list(
        self, t212_connector: Trading212Connector
    ) -> None:
        """fetch_accounts should return a list with one brokerage account."""
        await t212_connector.authenticate()
        accounts = await t212_connector.fetch_accounts()
        assert isinstance(accounts, list)
        assert len(accounts) == 1

        brokerage = accounts[0]
        assert brokerage.external_account_id == "12345678"
        assert brokerage.name == "Trading212"
        assert brokerage.account_type == "brokerage"
        assert brokerage.currency_code == "EUR"
        assert brokerage.current_balance == Decimal("10000.50")

    async def test_fetch_accounts_idempotent(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Calling fetch_accounts twice should be safe."""
        await t212_connector.authenticate()
        first = await t212_connector.fetch_accounts()
        second = await t212_connector.fetch_accounts()
        assert isinstance(first, list)
        assert isinstance(second, list)
        assert len(first) == len(second)

    async def test_fetch_accounts_not_authenticated(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Calling fetch_accounts before authenticate should raise."""
        with pytest.raises(PermanentError, match="not authenticated"):
            await t212_connector.fetch_accounts()

    # ── Portfolio ─────────────────────────────────────────────────────

    async def test_fetch_portfolio_returns_list(
        self, t212_connector: Trading212Connector
    ) -> None:
        """fetch_portfolio should return current holdings."""
        await t212_connector.authenticate()
        portfolio = await t212_connector.fetch_portfolio()
        assert isinstance(portfolio, list)
        assert len(portfolio) == 3  # AAPL, TSLA, VWCE.DE

        aapl = portfolio[0]
        assert aapl["ticker"] == "AAPL"
        assert aapl["quantity"] == 10.0
        assert aapl["averagePrice"] == 175.50

    async def test_fetch_portfolio_not_authenticated(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Calling fetch_portfolio before authenticate should raise."""
        with pytest.raises(PermanentError, match="not authenticated"):
            await t212_connector.fetch_portfolio()

    async def test_fetch_holdings_maps_portfolio_capability(
        self, t212_connector: Trading212Connector
    ) -> None:
        await t212_connector.authenticate()
        holdings = await t212_connector.fetch_holdings(account_id="12345678")

        assert "holdings" in t212_connector.supported_resources
        assert len(holdings) == 3
        assert holdings[0].security_reference.ticker == "AAPL"
        assert holdings[0].cost_basis == Decimal("1755.0")
        assert holdings[0].market_value == Decimal("1800.0")
        assert holdings[2].security_reference.security_type == "etf"
        assert len({holding.observed_at for holding in holdings}) == 1

    # ── Transactions ───────────────────────────────────────────────────

    async def test_fetch_transactions_returns_list(
        self, t212_connector: Trading212Connector
    ) -> None:
        """fetch_transactions should return a list of RawTransaction."""
        await t212_connector.authenticate()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        txns = await t212_connector.fetch_transactions(since=since)
        assert isinstance(txns, list)
        assert len(txns) >= 1

        # Verify first transaction
        txn = txns[0]
        assert isinstance(txn, RawTransaction)
        assert txn.external_transaction_id
        assert txn.external_account_id
        assert txn.amount is not None

    async def test_fetch_transactions_with_account_filter(
        self, t212_connector: Trading212Connector
    ) -> None:
        """fetch_transactions should accept an account_id filter."""
        await t212_connector.authenticate()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        txns = await t212_connector.fetch_transactions(
            since=since, account_id="12345678"
        )
        assert isinstance(txns, list)
        assert len(txns) >= 1

    async def test_fetch_transactions_with_limit(
        self, t212_connector: Trading212Connector
    ) -> None:
        """fetch_transactions should respect the limit parameter."""
        await t212_connector.authenticate()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        txns = await t212_connector.fetch_transactions(since=since, limit=3)
        assert isinstance(txns, list)
        assert len(txns) <= 3

    async def test_fetch_transactions_not_authenticated(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Calling fetch_transactions before authenticate should raise."""
        since = datetime(2024, 1, 1, tzinfo=UTC)
        with pytest.raises(PermanentError, match="not authenticated"):
            await t212_connector.fetch_transactions(since=since)

    async def test_fetch_transactions_contains_orders_and_dividends(
        self, t212_connector: Trading212Connector
    ) -> None:
        """Combined list should contain both orders and cash transactions."""
        await t212_connector.authenticate()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        txns = await t212_connector.fetch_transactions(since=since)

        types = {t.transaction_type for t in txns}
        # Should have various types from orders + cash transactions
        assert "purchase" in types  # BUY orders
        assert "sale" in types  # SELL orders
        assert "dividend" in types  # DIVIDEND transactions

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_accounts_roundtrip(
        self,
        t212_connector: Trading212Connector,
        sample_t212_raw_data: tuple[list, list],
    ) -> None:
        """Transform should map RawAccount to CanonicalAccountData."""
        raw_accounts, _ = sample_t212_raw_data
        if not raw_accounts:
            pytest.skip("No sample raw accounts provided")

        canonical = t212_connector.transform_accounts(raw_accounts)
        assert len(canonical) == len(raw_accounts)
        for ca in canonical:
            assert isinstance(ca, CanonicalAccountData)
            assert ca.provider_key == "trading212"
            assert ca.external_account_id
            assert ca.account_type

    async def test_transform_transactions_roundtrip(
        self,
        t212_connector: Trading212Connector,
        sample_t212_raw_data: tuple[list, list],
    ) -> None:
        """Transform should map RawTransaction to CanonicalTransactionData."""
        _, raw_txns = sample_t212_raw_data
        if not raw_txns:
            pytest.skip("No sample raw transactions provided")

        canonical = t212_connector.transform_transactions(raw_txns)
        assert len(canonical) == len(raw_txns)
        for ct in canonical:
            assert isinstance(ct, CanonicalTransactionData)
            assert ct.provider_key == "trading212"
            assert ct.external_transaction_id
            assert ct.transaction_type
            assert ct.status

    async def test_transform_transactions_maps_datamodel_fields(
        self,
        t212_connector: Trading212Connector,
    ) -> None:
        """Full fetch → transform pipeline populates every datamodel field.

        Acceptance criterion: running the fetch step produces a list of
        Transaction objects matching the finance-sync Transaction
        datamodel schema (ticker, type, quantity, price, currency, fees,
        executed_at, account_id, external_id) with no data loss.
        """
        from tests.connectors.fixtures.trading212_api_fixtures import (
            DIVIDEND_AAPL,
            ORDER_BUY_AAPL,
        )

        raw = [
            _parse_order(ORDER_BUY_AAPL, "12345678"),
            _parse_cash_transaction(DIVIDEND_AAPL, "12345678"),
        ]
        canonical = t212_connector.transform_transactions(raw)
        assert len(canonical) == 2

        order, dividend = canonical

        # Order: full datamodel mapping
        assert order.external_transaction_id == "order_10000001"
        assert order.external_account_id == "12345678"
        assert order.transaction_type == "purchase"
        assert order.quantity == Decimal(10)
        assert order.unit_price == Decimal("175.50")
        assert order.currency_code == "EUR"
        assert order.occurred_at is not None  # executed_at
        assert order.status == "booked"
        # Ticker is carried on the security reference
        assert order.security_reference is not None
        assert order.security_reference.ticker == "AAPL"
        assert order.security_reference.external_id == "AAPL"

        # Dividend: no data loss, type + amount intact
        assert dividend.external_transaction_id == "txn_20000001"
        assert dividend.transaction_type == "dividend"
        assert dividend.amount == Decimal("15.00")
        assert dividend.security_reference is not None
        assert dividend.security_reference.ticker == "AAPL"

    # ── Name ───────────────────────────────────────────────────────────

    async def test_name_is_string(
        self, t212_connector: Trading212Connector
    ) -> None:
        """The name property should return a non-empty string."""
        assert isinstance(t212_connector.name, str)
        assert len(t212_connector.name) > 0
        assert t212_connector.name == "trading212"
        assert t212_connector.name == t212_connector.config.provider_type

    async def test_display_name(
        self, t212_connector: Trading212Connector
    ) -> None:
        """display_name should be set."""
        assert t212_connector.display_name == "Trading212"

    async def test_sdk_version(
        self, t212_connector: Trading212Connector
    ) -> None:
        """sdk_version should be a valid string."""
        assert t212_connector.sdk_version == "0.1.0"


# ═══════════════════════════════════════════════════════════════════════
# Unit tests — Trading212-specific logic
# ═══════════════════════════════════════════════════════════════════════


class TestTrading212ConnectorAuth:
    """Authentication-specific behaviour."""

    pytestmark = pytest.mark.asyncio

    async def test_authenticate_sets_account_id_and_currency(
        self,
        t212_connector: Trading212Connector,
        t212_mock_transport: object,
    ) -> None:
        """After authenticate, the connector should have account ID."""
        assert t212_connector._account_id is None
        assert t212_connector._account_currency == "EUR"

        await t212_connector.authenticate()

        assert t212_connector._account_id == "12345678"
        assert t212_connector._account_currency == "EUR"


class TestTrading212ConnectorPagination:
    """Pagination for order and transaction history."""

    pytestmark = pytest.mark.asyncio

    async def test_history_requests_never_exceed_api_cap(
        self,
        t212_connector_config: ConnectorConfig,
        t212_mock_transport: object,
    ) -> None:
        """History requests must never send ``limit`` above the API cap.

        Regression test for issue #505: ``_DEFAULT_PAGE_SIZE`` was 100,
        but Trading212 rejects ``limit > 50`` with HTTP 400
        ``{"detail": "Limit cannot be greater than 50"}``, which aborted
        the whole sync.  Every request to /history/orders and
        /history/transactions must carry ``limit <= 50``.
        """
        import httpx

        from finance_sync.connectors.trading212 import (
            _DEFAULT_PAGE_SIZE,
            Trading212Connector,
        )

        assert _DEFAULT_PAGE_SIZE <= 50

        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com",
            transport=t212_mock_transport,  # type: ignore[arg-type]
        )
        conn = Trading212Connector(
            config=t212_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        conn._account_id = "12345678"
        since = datetime(2024, 1, 1, tzinfo=UTC)

        # Fetch with no explicit limit -> page size must be the default (50)
        await conn._fetch_order_history("test_t212_api_key_abc123", since, None)
        await conn._fetch_transaction_history(
            "test_t212_api_key_abc123", since, None
        )

        # Fetch with an explicit limit above the cap -> clamped to 50
        await conn._fetch_order_history(
            "test_t212_api_key_abc123", since, limit=1000
        )

        history_urls = [
            str(call["url"])
            for call in t212_mock_transport.call_log  # type: ignore[attr-defined]
            if "/history/" in str(call["url"])
        ]
        assert history_urls, "expected history requests to be recorded"
        for url in history_urls:
            # httpx keeps 'limit=50' as the raw query fragment
            assert "limit=50" in url, f"limit exceeds API cap in {url}"

    async def test_order_history_pagination(
        self,
        t212_connector_config: ConnectorConfig,
        t212_mock_transport: object,
    ) -> None:
        """Connector should follow pagination links for orders."""
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com",
            transport=t212_mock_transport,  # type: ignore[arg-type]
        )
        conn = Trading212Connector(
            config=t212_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        # Force account id so _fetch_order_history works
        conn._account_id = "12345678"
        since = datetime(2024, 1, 1, tzinfo=UTC)
        txns = await conn._fetch_order_history(
            "test_t212_api_key_abc123", since, limit=100
        )
        # Should have orders from both pages
        assert len(txns) == 4  # all 4 orders from both pages

    async def test_transaction_history_pagination(
        self,
        t212_connector_config: ConnectorConfig,
        t212_mock_transport: object,
    ) -> None:
        """Connector should follow pagination links for transactions."""
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com",
            transport=t212_mock_transport,  # type: ignore[arg-type]
        )
        conn = Trading212Connector(
            config=t212_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        conn._account_id = "12345678"
        since = datetime(2024, 1, 1, tzinfo=UTC)
        txns = await conn._fetch_transaction_history(
            "test_t212_api_key_abc123", since, limit=100
        )
        # Should have transactions from both pages
        assert len(txns) == 6  # all 6 transactions from both pages

    async def test_fetch_transactions_with_date_range(
        self,
        t212_connector_config: ConnectorConfig,
        t212_mock_transport: object,
    ) -> None:
        """fetch_transactions should pass ``to`` as a query parameter.

        The ``to`` parameter bounds the upper end of the window, so a
        date-range fetch only ever returns the first page of fixtures.
        """
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com",
            transport=t212_mock_transport,  # type: ignore[arg-type]
        )
        conn = Trading212Connector(
            config=t212_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        conn._account_id = "12345678"
        since = datetime(2024, 1, 1, tzinfo=UTC)
        to = datetime(2024, 6, 30, tzinfo=UTC)

        txns = await conn.fetch_transactions(since=since, to=to)
        assert isinstance(txns, list)

        urls = [
            str(call["url"])
            for call in t212_mock_transport.call_log  # type: ignore[attr-defined]
        ]
        history_urls = [u for u in urls if "/history/" in u]
        assert history_urls, "expected history requests to be recorded"
        # The first (non-cursor) request carries both from= and to=;
        # follow-up pages use the provider's own cursor URL unchanged.
        initial = [u for u in history_urls if "cursor=" not in u]
        assert initial, "expected an initial history request"
        for url in initial:
            assert "from=2024-01-01T00:00:00.000Z" in url
            assert "to=2024-06-30T00:00:00.000Z" in url, f"missing to= in {url}"

    async def test_fetch_transactions_invalid_date_range(
        self,
        t212_connector_config: ConnectorConfig,
        t212_mock_transport: object,
    ) -> None:
        """A ``to`` at or before ``since`` must raise PermanentError."""
        import httpx

        from finance_sync.connectors.exceptions import PermanentError
        from finance_sync.connectors.trading212 import Trading212Connector

        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com",
            transport=t212_mock_transport,  # type: ignore[arg-type]
        )
        conn = Trading212Connector(
            config=t212_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        conn._account_id = "12345678"
        since = datetime(2024, 1, 1, tzinfo=UTC)

        with pytest.raises(PermanentError, match="date range invalid"):
            await conn.fetch_transactions(
                since=since, to=datetime(2024, 1, 1, tzinfo=UTC)
            )
        with pytest.raises(PermanentError, match="date range invalid"):
            await conn.fetch_transactions(
                since=since, to=datetime(2023, 12, 31, tzinfo=UTC)
            )

    async def test_fetch_transactions_deduplicates_by_external_id(
        self,
        t212_connector_config: ConnectorConfig,
        t212_mock_transport: object,
    ) -> None:
        """Duplicate external ids across endpoints must be collapsed.

        The connector merges order history and cash transaction history;
        a defensive dedup pass guarantees the acceptance criterion
        "no duplicates" holds at the fetch layer even if the provider
        ever returns the same id from both endpoints.
        """
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com",
            transport=t212_mock_transport,  # type: ignore[arg-type]
        )
        conn = Trading212Connector(
            config=t212_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        conn._account_id = "12345678"
        since = datetime(2024, 1, 1, tzinfo=UTC)

        txns = await conn.fetch_transactions(since=since)
        ids = [t.external_transaction_id for t in txns]
        assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"

        # Orders use order_ prefix, cash transactions txn_ prefix
        prefixes = {i.split("_")[0] for i in ids}
        assert prefixes == {"order", "txn"}


class TestTrading212DatetimeParsing:
    """Trading212-specific datetime parsing."""

    def test_parse_full_datetime(self) -> None:
        """Parse a Trading212 datetime with milliseconds."""
        dt = _parse_t212_datetime("2024-01-15T10:00:00.000Z")
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.minute == 0
        assert dt.second == 0
        assert dt.microsecond == 0
        assert dt.tzinfo is not None

    def test_parse_datetime_no_milliseconds(self) -> None:
        """Parse a Trading212 datetime without milliseconds."""
        dt = _parse_t212_datetime("2024-06-20T14:00:00Z")
        assert dt.year == 2024
        assert dt.month == 6
        assert dt.day == 20
        assert dt.hour == 14
        assert dt.microsecond == 0
        assert dt.tzinfo is not None

    def test_parse_datetime_with_microseconds(self) -> None:
        """Parse a Trading212 datetime with microseconds."""
        dt = _parse_t212_datetime("2025-06-15T14:30:00.123456Z")
        assert dt.microsecond == 123456
        assert dt.tzinfo is not None

    def test_parse_empty_string(self) -> None:
        """Empty string should return epoch."""
        dt = _parse_t212_datetime("")
        assert dt.tzinfo is not None
        assert dt.replace(tzinfo=UTC).timestamp() == 0.0

    def test_parse_none(self) -> None:
        """None should return epoch."""
        dt = _parse_t212_datetime(None)
        assert dt.tzinfo is not None
        assert dt.replace(tzinfo=UTC).timestamp() == 0.0


class TestTrading212OrderParsing:
    """Order parsing (buy/sell orders from /history/orders)."""

    def test_parse_buy_order(self) -> None:
        """A BUY order should parse as a purchase with negative amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_BUY_AAPL,
        )

        txn = _parse_order(ORDER_BUY_AAPL, "12345678")
        assert txn.external_transaction_id == "order_10000001"
        assert txn.external_account_id == "12345678"
        assert txn.amount == Decimal("-1755.00")  # outflow
        assert txn.currency_code == "EUR"
        assert txn.transaction_type == "purchase"
        assert txn.status == "booked"
        assert txn.description == "BUY 10.0 x AAPL"

    def test_parse_sell_order(self) -> None:
        """A SELL order should parse as a sale with positive amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_SELL_TSLA,
        )

        txn = _parse_order(ORDER_SELL_TSLA, "12345678")
        assert txn.external_transaction_id == "order_10000002"
        assert txn.amount == Decimal("540.00")  # inflow
        assert txn.transaction_type == "sale"
        assert txn.status == "booked"
        assert txn.description == "SELL 2.0 x TSLA"

    def test_parse_pending_order(self) -> None:
        """A pending order should have pending status."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_PENDING,
        )

        txn = _parse_order(ORDER_PENDING, "12345678")
        assert txn.status == "pending"
        assert txn.transaction_type == "purchase"

    def test_parse_order_metadata(self) -> None:
        """Provider metadata should contain order details."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_BUY_AAPL,
        )

        txn = _parse_order(ORDER_BUY_AAPL, "12345678")
        meta = txn.provider_metadata or {}
        assert meta.get("ticker") == "AAPL"
        assert meta.get("side") == "BUY"
        assert meta.get("order_type") == "MARKET"
        assert meta.get("filled_price") == 175.50
        assert meta.get("quantity") == 10.0

    def test_parse_order_populates_unit_price(self) -> None:
        """Orders should surface the filled price as unit_price."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_BUY_AAPL,
        )

        txn = _parse_order(ORDER_BUY_AAPL, "12345678")
        assert txn.unit_price == Decimal("175.50")

    def test_parse_order_with_fees(self) -> None:
        """Tax + stamp duty should surface as a positive fee_amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_BUY_AAPL,
        )

        order_with_fees = dict(ORDER_BUY_AAPL)
        order_with_fees["tax"] = 1.50
        order_with_fees["stampDuty"] = 2.00

        txn = _parse_order(order_with_fees, "12345678")
        assert txn.fee_amount == Decimal("3.50")
        assert txn.fee_currency_code == "EUR"

    def test_parse_order_without_fees(self) -> None:
        """Orders without tax/stamp duty should have no fee_amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_BUY_AAPL,
        )

        txn = _parse_order(ORDER_BUY_AAPL, "12345678")
        assert txn.fee_amount is None
        assert txn.fee_currency_code is None

    def test_parse_pending_order_no_fees(self) -> None:
        """A pending order has no filled price, so unit_price is None."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            ORDER_PENDING,
        )

        txn = _parse_order(ORDER_PENDING, "12345678")
        assert txn.unit_price is None
        assert txn.fee_amount is None


class TestTrading212CashTransactionParsing:
    """Cash transaction parsing (dividends, deposits, etc.)."""

    def test_parse_dividend(self) -> None:
        """A dividend should parse with positive amount and dividend type."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            DIVIDEND_AAPL,
        )

        txn = _parse_cash_transaction(DIVIDEND_AAPL, "12345678")
        assert txn.external_transaction_id == "txn_20000001"
        assert txn.amount == Decimal("15.00")  # inflow
        assert txn.transaction_type == "dividend"
        assert txn.status == "booked"
        assert "AAPL" in (txn.description or "")

    def test_parse_deposit(self) -> None:
        """A deposit should parse with positive amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            DEPOSIT_1,
        )

        txn = _parse_cash_transaction(DEPOSIT_1, "12345678")
        assert txn.amount == Decimal("5000.00")
        assert txn.transaction_type == "deposit"

    def test_parse_withdrawal(self) -> None:
        """A withdrawal should parse with negative amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            WITHDRAWAL_1,
        )

        txn = _parse_cash_transaction(WITHDRAWAL_1, "12345678")
        assert txn.amount == Decimal("-1000.00")
        assert txn.transaction_type == "withdrawal"

    def test_parse_interest(self) -> None:
        """Interest should parse with positive amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            INTEREST_1,
        )

        txn = _parse_cash_transaction(INTEREST_1, "12345678")
        assert txn.amount == Decimal("12.34")
        assert txn.transaction_type == "interest"

    def test_parse_fee(self) -> None:
        """A fee should parse with negative amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            FEE_1,
        )

        txn = _parse_cash_transaction(FEE_1, "12345678")
        assert txn.amount == Decimal("-2.50")
        assert txn.transaction_type == "fee"
        assert txn.fee_amount == Decimal("2.50")
        assert txn.fee_currency_code == "EUR"

    def test_parse_dividend_no_fee(self) -> None:
        """A dividend should not carry a fee_amount."""
        from tests.connectors.fixtures.trading212_api_fixtures import (
            DIVIDEND_AAPL,
        )

        txn = _parse_cash_transaction(DIVIDEND_AAPL, "12345678")
        assert txn.fee_amount is None
        assert txn.fee_currency_code is None


class TestTrading212Mapping:
    """Transaction type and status mapping."""

    def test_map_order_sides(self) -> None:
        """Order sides should map to canonical types correctly."""
        assert _map_order_side("BUY") == "purchase"
        assert _map_order_side("SELL") == "sale"
        assert _map_order_side("UNKNOWN") == "other"
        assert _map_order_side("") == "other"

    def test_map_order_sides_case_insensitive(self) -> None:
        """Order side mapping should be case-insensitive."""
        assert _map_order_side("buy") == "purchase"
        assert _map_order_side("Sell") == "sale"

    def test_map_order_statuses(self) -> None:
        """Order statuses should map to canonical statuses correctly."""
        assert _map_order_status("FILLED") == "booked"
        assert _map_order_status("PENDING") == "pending"
        assert _map_order_status("CANCELLED") == "cancelled"
        assert _map_order_status("REJECTED") == "cancelled"
        assert _map_order_status("PARTIALLY_FILLED") == "pending"
        assert _map_order_status("UNKNOWN") == "pending"

    def test_map_order_status_case_insensitive(self) -> None:
        """Status mapping should be case-insensitive."""
        assert _map_order_status("filled") == "booked"
        assert _map_order_status("Pending") == "pending"

    def test_map_transaction_types(self) -> None:
        """Cash transaction types should map to canonical types."""
        assert _map_transaction_type("DIVIDEND") == "dividend"
        assert _map_transaction_type("DEPOSIT") == "deposit"
        assert _map_transaction_type("WITHDRAWAL") == "withdrawal"
        assert _map_transaction_type("INTEREST") == "interest"
        assert _map_transaction_type("FEE") == "fee"
        assert _map_transaction_type("TAX") == "tax"
        assert _map_transaction_type("CASHBACK") == "deposit"
        assert _map_transaction_type("LOYALTY_BONUS") == "interest"
        assert _map_transaction_type("UNKNOWN") == "other"

    def test_map_transaction_types_case_insensitive(self) -> None:
        """Transaction type mapping should be case-insensitive."""
        assert _map_transaction_type("dividend") == "dividend"
        assert _map_transaction_type("Deposit") == "deposit"


class TestTrading212ConnectorErrorHandling:
    """Error classification and handling."""

    pytestmark = pytest.mark.asyncio

    async def test_rate_limit_error(
        self,
        t212_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 429 should raise RateLimitError."""
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(429, json={"error": "Too Many Requests"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com", transport=transport
        )
        conn = Trading212Connector(
            config=t212_connector_config, http_client=http_client
        )

        with pytest.raises(RateLimitError):
            await conn.authenticate()

    async def test_authentication_error(
        self,
        t212_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 401 should raise PermanentError."""
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(401, json={"error": "Unauthorized"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com", transport=transport
        )
        conn = Trading212Connector(
            config=t212_connector_config, http_client=http_client
        )

        with pytest.raises(PermanentError, match="authentication failed"):
            await conn.authenticate()

    async def test_forbidden_error(
        self,
        t212_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 403 should raise PermanentError."""
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(403, json={"error": "Forbidden"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com", transport=transport
        )
        conn = Trading212Connector(
            config=t212_connector_config, http_client=http_client
        )

        with pytest.raises(PermanentError, match="authentication failed"):
            await conn.authenticate()

    async def test_server_error(
        self,
        t212_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 500 should raise TransientError."""
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(500, json={"error": "Internal Server Error"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com", transport=transport
        )
        conn = Trading212Connector(
            config=t212_connector_config, http_client=http_client
        )

        with pytest.raises(TransientError):
            await conn.authenticate()

    async def test_bad_request_error_is_permanent(
        self,
        t212_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 400 should raise PermanentError — never retried.

        Regression test for the prod sync failure where Trading212
        answered ``400 Bad Request`` to the history request and the
        connector classified it as transient, burning all 3 retries and
        surfacing as the misleading "All 3 retries exhausted" alert.
        A 400 is a client/contract error: retrying it can never succeed,
        so it must fail fast as PermanentError.
        """
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(400, json={"error": "Bad Request"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com", transport=transport
        )
        conn = Trading212Connector(
            config=t212_connector_config, http_client=http_client
        )

        with pytest.raises(PermanentError, match="HTTP 400"):
            await conn.authenticate()

    async def test_bad_request_during_fetch_not_retried(
        self,
        t212_connector_config: ConnectorConfig,
    ) -> None:
        """A 400 during a rate-limited fetch fails fast without retries.

        The orchestrator calls ``_rate_limited_fetch_transactions`` which
        wraps the fetch in ``RateLimiter.retry``; a PermanentError must
        propagate immediately (no retry loop, no "retries exhausted"
        wrap) so the actionable status code reaches the sync result.
        """
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            path = request.url.path
            if path == "/api/v0/equity/account/cash":
                return httpx.Response(200, json={"free": 100.0})
            if path == "/api/v0/equity/account/info":
                return httpx.Response(200, json={"id": "12345678"})
            # history endpoints -> 400
            return httpx.Response(400, json={"error": "Bad Request"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com", transport=transport
        )
        conn = Trading212Connector(
            config=t212_connector_config, http_client=http_client
        )
        await conn.authenticate()
        conn._account_id = "12345678"

        from datetime import UTC, datetime

        with pytest.raises(PermanentError, match="HTTP 400"):
            await conn._rate_limited_fetch_transactions(
                datetime(2024, 1, 1, tzinfo=UTC)
            )
        # auth: 2 requests (cash + info) + exactly 1 history attempt: no retry
        assert calls["count"] == 3

    async def test_not_found_is_permanent(
        self,
        t212_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 404 should raise PermanentError, not TransientError."""
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(404, json={"error": "Not Found"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com", transport=transport
        )
        conn = Trading212Connector(
            config=t212_connector_config, http_client=http_client
        )

        with pytest.raises(PermanentError, match="HTTP 404"):
            await conn.authenticate()

    async def test_transient_error_on_fetch(
        self,
        t212_connector: Trading212Connector,
        t212_mock_transport: object,
    ) -> None:
        """After auth succeeds, verify error classification works."""
        await t212_connector.authenticate()

        import httpx

        from finance_sync.connectors.trading212 import _raise_for_status

        with pytest.raises(RateLimitError):
            resp = httpx.Response(429)
            _raise_for_status(resp)

        with pytest.raises(PermanentError):
            resp = httpx.Response(401)
            _raise_for_status(resp)

        with pytest.raises(PermanentError):
            resp = httpx.Response(403)
            _raise_for_status(resp)

        with pytest.raises(PermanentError, match="HTTP 400"):
            resp = httpx.Response(400)
            _raise_for_status(resp)

        with pytest.raises(PermanentError, match="HTTP 404"):
            resp = httpx.Response(404)
            _raise_for_status(resp)

        with pytest.raises(TransientError):
            resp = httpx.Response(503)
            _raise_for_status(resp)

    def test_rate_limit_policy_retries_transient_errors(self) -> None:
        """The Trading212 policy must retry transient failures robustly.

        The observed production failure was a transient provider outage
        that exhausted the old ``max_retries=3`` / ``backoff_base=1.0``
        policy after ~7 seconds.  The policy now retries longer so a
        brief outage does not surface as a sync failure.
        """
        from finance_sync.connectors.trading212 import Trading212Connector

        policy = Trading212Connector.rate_limit_policy
        assert policy is not None
        assert policy.max_retries >= 5
        assert policy.backoff_base >= 2.0

    async def test_account_id_mismatch_returns_empty(
        self, t212_connector: Trading212Connector
    ) -> None:
        """fetch_transactions with non-matching account_id returns empty."""
        await t212_connector.authenticate()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        txns = await t212_connector.fetch_transactions(
            since=since, account_id="nonexistent_account"
        )
        assert txns == []

    async def test_validated_since_reaches_from_param(
        self,
        t212_connector_config: ConnectorConfig,
        t212_mock_transport: object,
    ) -> None:
        """The validated ``since`` is exactly what the provider call sends.

        End-to-end guarantee for the connection sync path: a ``since``
        that the orchestrator validated (here a truncated ISO string,
        interpreted as UTC) must reach the Trading212 API as the
        ``from=`` query parameter — never a raw string or a naive
        timestamp that could shift the look-back window.
        """
        import httpx

        from finance_sync.connectors.trading212 import Trading212Connector
        from finance_sync.sync.errors import validate_since

        transport = t212_mock_transport
        http_client = httpx.AsyncClient(
            base_url="https://live.trading212.com",
            transport=transport,  # type: ignore[arg-type]
        )
        conn = Trading212Connector(
            config=t212_connector_config,
            http_client=http_client,
        )
        await conn.authenticate()
        conn._account_id = "12345678"

        # What the orchestrator's run_sync would hand the connector after
        # validate_since(): an aware UTC datetime parsed from a truncated
        # ISO string.
        validated = validate_since("2026-05-29T13:04:07")

        txns = await conn.fetch_transactions(since=validated)
        assert isinstance(txns, list)

        # The connector formatted the validated (UTC-aware) datetime.
        # Its strftime produces ``2026-05-29T13:04:07.000Z`` (milliseconds
        # + Z suffix; httpx leaves ``:``/``.`` literal in the query) — the
        # point is that the value is the validated UTC instant, not a raw
        # string or a naive timestamp.
        expected_fragment = "from=2026-05-29T13:04:07.000Z"
        urls = [
            str(call["url"])
            for call in transport.call_log  # type: ignore[attr-defined]
        ]
        history_urls = [u for u in urls if "/history/" in u]
        assert history_urls, "expected history requests to be recorded"
        assert any(expected_fragment in u for u in history_urls), (
            f"expected {expected_fragment!r} in {history_urls}"
        )
