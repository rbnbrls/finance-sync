"""Tests for connector Pydantic models."""
# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    ConnectorHealth,
    RawAccount,
    RawTransaction,
)


class TestRawAccount:
    """RawAccount model validation."""

    def test_minimal(self) -> None:
        account = RawAccount(
            external_account_id="acc_1",
            name="Test Account",
            account_type="checking",
        )
        assert account.external_account_id == "acc_1"
        assert account.currency_code == "EUR"  # default
        assert account.current_balance is None
        assert account.provider_metadata is None

    def test_full(self, sample_raw_account: RawAccount) -> None:
        assert sample_raw_account.iso_currency_code == "EUR"
        assert sample_raw_account.provider_metadata is not None
        assert "iban" in sample_raw_account.provider_metadata


class TestRawTransaction:
    """RawTransaction model validation."""

    def test_minimal(self) -> None:
        txn = RawTransaction(
            external_transaction_id="tx_1",
            external_account_id="acc_1",
            amount=Decimal(100),
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert txn.currency_code == "EUR"
        assert txn.transaction_type is None
        assert txn.status is None

    def test_full(self, sample_raw_transaction: RawTransaction) -> None:
        assert sample_raw_transaction.amount == Decimal("-42.50")
        assert sample_raw_transaction.transaction_type == "purchase"


class TestCanonicalAccountData:
    """CanonicalAccountData model validation."""

    def test_minimal(self) -> None:
        acc = CanonicalAccountData(
            provider_key="test",
            external_account_id="acc_1",
            name="Test",
            account_type="savings",
        )
        assert acc.is_active is True
        assert acc.currency_code == "EUR"


class TestCanonicalTransactionData:
    """CanonicalTransactionData model validation."""

    def test_minimal(self) -> None:
        txn = CanonicalTransactionData(
            provider_key="test",
            external_transaction_id="tx_1",
            external_account_id="acc_1",
            amount=Decimal(-50),
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
            transaction_type="transfer",
        )
        assert txn.status == "pending"  # default


class TestConnectorConfig:
    """ConnectorConfig validation."""

    def test_defaults(self) -> None:
        config = ConnectorConfig(provider_type="bunq")
        assert config.credentials == {}
        assert config.options == {}

    def test_with_credentials(self) -> None:
        config = ConnectorConfig(
            provider_type="bunq",
            credentials={"api_key": "secret123"},
            options={"sandbox": True},
        )
        assert config.credentials["api_key"] == "secret123"
        assert config.options["sandbox"] is True


class TestConnectorHealth:
    """ConnectorHealth validation."""

    def test_healthy(self) -> None:
        h = ConnectorHealth(healthy=True, provider_type="bunq")
        assert h.healthy
        assert h.message is None

    def test_unhealthy(self) -> None:
        h = ConnectorHealth(
            healthy=False,
            message="Connection refused",
            provider_type="bunq",
        )
        assert not h.healthy
        assert h.message == "Connection refused"
