"""pytest configuration and shared fixtures for finance-sync tests."""
# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.rate_limiter import RateLimiter, RateLimitPolicy

# ── Sample data factories ──────────────────────────────────────────────


@pytest.fixture
def sample_raw_account() -> RawAccount:
    """Return a typical raw account fixture."""
    return RawAccount(
        external_account_id="acc_12345",
        name="Main Checking",
        account_type="checking",
        account_subtype=None,
        currency_code="EUR",
        current_balance=Decimal("1520.45"),
        available_balance=Decimal("1480.00"),
        iso_currency_code="EUR",
        provider_metadata={"iban": "NL00BANK0123456789", "bic": "BANKNL2A"},
    )


@pytest.fixture
def sample_raw_transaction() -> RawTransaction:
    """Return a typical raw transaction fixture."""
    return RawTransaction(
        external_transaction_id="txn_98765",
        external_account_id="acc_12345",
        amount=Decimal("-42.50"),
        currency_code="EUR",
        occurred_at=datetime(2025, 6, 1, 12, 30, 0, tzinfo=UTC),
        booked_at=datetime(2025, 6, 1, 14, 0, 0, tzinfo=UTC),
        description="Coffee shop",
        transaction_type="purchase",
        status="booked",
        provider_fingerprint="hash_abc123",
    )


@pytest.fixture
def sample_connector_config() -> ConnectorConfig:
    """Return a connector config for the mock provider."""
    return ConnectorConfig(
        provider_type="mock_provider",
        credentials={"api_key": "test_key_123"},
        options={"sandbox": True},
    )


# ── Mock connector for testing ─────────────────────────────────────────


class MockConnector(Connector):
    """A minimal connector implementation for use in contract tests.

    Exposes the raw data passed in constructor.  Can be configured to
    raise specific errors for error-classification tests.
    """

    display_name = "Mock Provider (Test)"

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        accounts: list[RawAccount] | None = None,
        transactions: list[RawTransaction] | None = None,
        fail_auth: bool = False,
        fail_accounts: bool = False,
        fail_transactions: bool = False,
        transient_errors: bool = False,
        rate_limit_policy: RateLimitPolicy | None = None,
    ) -> None:
        super().__init__(config)
        self._accounts = accounts or []
        self._transactions = transactions or []
        self._fail_auth = fail_auth
        self._fail_accounts = fail_accounts
        self._fail_transactions = fail_transactions
        self._transient_errors = transient_errors
        # Override the class-level policy if provided
        if rate_limit_policy is not None:
            self.rate_limit_policy = rate_limit_policy
            self._rate_limiter = RateLimiter(rate_limit_policy)
        self.auth_calls = 0
        self.fetch_accounts_calls = 0
        self.fetch_transactions_calls = 0

    @property
    def name(self) -> str:
        return self.config.provider_type

    async def authenticate(self) -> None:
        self.auth_calls += 1
        if self._fail_auth:
            if self._transient_errors:
                from finance_sync.connectors.exceptions import TransientError

                msg = "Mock provider unavailable"
                raise TransientError(msg)
            from finance_sync.connectors.exceptions import PermanentError

            msg = "Mock auth failed"
            raise PermanentError(msg)

    async def fetch_accounts(self) -> list[RawAccount]:
        self.fetch_accounts_calls += 1
        if self._fail_accounts:
            if self._transient_errors:
                from finance_sync.connectors.exceptions import TransientError

                msg = "Mock accounts unavailable"
                raise TransientError(msg)
            from finance_sync.connectors.exceptions import PermanentError

            msg = "Mock accounts error"
            raise PermanentError(msg)
        return self._accounts

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        self.fetch_transactions_calls += 1
        if self._fail_transactions:
            if self._transient_errors:
                from finance_sync.connectors.exceptions import TransientError

                msg = "Mock transactions unavailable"
                raise TransientError(msg)
            from finance_sync.connectors.exceptions import PermanentError

            msg = "Mock transactions error"
            raise PermanentError(msg)

        filtered = self._transactions
        if account_id:
            filtered = [
                t for t in filtered if t.external_account_id == account_id
            ]
        if limit and limit < len(filtered):
            filtered = filtered[:limit]

        # Filter by 'since'
        return [t for t in filtered if t.occurred_at >= since]


@pytest.fixture
def mock_connector(
    sample_connector_config: ConnectorConfig,
    sample_raw_account: RawAccount,
    sample_raw_transaction: RawTransaction,
) -> MockConnector:
    """Return a fully functional MockConnector with sample data."""
    return MockConnector(
        config=sample_connector_config,
        accounts=[sample_raw_account],
        transactions=[sample_raw_transaction],
    )


# ── ConnectorRegistry with pre-registered mock ─────────────────────────


@pytest.fixture
def registry_with_mock() -> tuple:
    """Return a registry with MockConnector registered and its config."""
    from finance_sync.connectors.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    registry.register_class("mock_provider", MockConnector)
    config = ConnectorConfig(
        provider_type="mock_provider",
        credentials={"api_key": "test_key_123"},
        options={"sandbox": True},
    )
    return registry, config
