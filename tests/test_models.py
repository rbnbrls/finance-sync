"""Tests for SQLAlchemy ORM model instantiation, repr, and enum usage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from finance_sync.models import (
    Account,
    Balance,
    Holding,
    OutboxMessage,
    Security,
    SecurityListing,
    SyncRun,
    Tenant,
    Transaction,
    User,
)
from finance_sync.models.enums import (
    AccountType,
    BalanceKind,
    BalanceSource,
    HoldingSource,
    SecurityType,
    SyncRunStatus,
    TransactionStatus,
    TransactionType,
    UserRole,
)


class TestTenantModel:
    def test_instantiate(self) -> None:
        tenant = Tenant(slug="test-tenant", name="Test Tenant")
        assert tenant.slug == "test-tenant"
        assert tenant.name == "Test Tenant"

    def test_repr_contains_slug(self) -> None:
        tenant = Tenant(slug="t1", name="T1")
        assert "Tenant" in repr(tenant)
        assert "t1" in repr(tenant)


class TestUserModel:
    def test_instantiate(self) -> None:
        user = User(
            tenant_id=uuid4(),
            email="test@example.com",
            hashed_password="hash123",
            display_name="Test User",
            role=UserRole.VIEWER,
            is_active=True,
        )
        assert user.email == "test@example.com"
        assert user.role == UserRole.VIEWER
        assert user.is_active is True
        assert user.display_name == "Test User"

    def test_repr(self) -> None:
        user = User(
            tenant_id=uuid4(),
            email="a@b.com",
            hashed_password="h",
            display_name="A",
            is_active=True,
        )
        assert "User" in repr(user)


class TestAccountModel:
    def test_instantiate(self) -> None:
        account = Account(
            tenant_id=uuid4(),
            provider_key="plaid",
            external_account_id="acc_123",
            name="Checking",
            account_type=AccountType.CHECKING,
            currency_code="EUR",
        )
        assert account.name == "Checking"
        assert account.account_type == AccountType.CHECKING
        assert account.currency_code == "EUR"

    def test_with_metadata(self) -> None:
        account = Account(
            tenant_id=uuid4(),
            provider_key="teller",
            external_account_id="acc_456",
            name="Savings",
            account_type=AccountType.SAVINGS,
            currency_code="EUR",
            provider_metadata={"iban": "NL00BANK0123456789"},
        )
        assert account.provider_metadata == {"iban": "NL00BANK0123456789"}

    def test_repr(self) -> None:
        account = Account(
            tenant_id=uuid4(),
            provider_key="p",
            external_account_id="e",
            name="A",
            account_type=AccountType.OTHER,
        )
        assert "Account" in repr(account)
        assert "A" in repr(account)


class TestSecurityModel:
    def test_instantiate(self) -> None:
        sec = Security(
            isin="US0378331005",
            name="Apple Inc.",
            security_type=SecurityType.STOCK,
            ticker="AAPL",
            currency_code="USD",
        )
        assert sec.isin == "US0378331005"
        assert sec.name == "Apple Inc."
        assert sec.security_type == SecurityType.STOCK
        assert sec.ticker == "AAPL"
        assert sec.currency_code == "USD"

    def test_repr(self) -> None:
        sec = Security(
            isin="US0378331005",
            name="AAPL",
            security_type=SecurityType.ETF,
        )
        assert "Security" in repr(sec)
        assert "AAPL" in repr(sec)


class TestSecurityListingModel:
    def test_instantiate(self) -> None:
        listing = SecurityListing(
            security_id=uuid4(),
            mic="XNYS",
            ticker="AAPL",
            currency_code="USD",
            is_primary_listing=True,
        )
        assert listing.mic == "XNYS"
        assert listing.ticker == "AAPL"
        assert listing.is_primary_listing is True

    def test_repr(self) -> None:
        listing = SecurityListing(
            security_id=uuid4(),
            mic="XAMS",
            ticker="AAPL",
            currency_code="EUR",
        )
        assert "SecurityListing" in repr(listing)
        assert "XAMS" in repr(listing)


class TestTransactionModel:
    def test_instantiate(self) -> None:
        now = datetime.now(UTC)
        txn = Transaction(
            tenant_id=uuid4(),
            provider_key="plaid",
            external_transaction_id="txn_1",
            account_id=uuid4(),
            amount=Decimal("-42.50"),
            currency_code="EUR",
            occurred_at=now,
            transaction_type=TransactionType.PURCHASE,
            status=TransactionStatus.BOOKED,
            revision=1,
        )
        assert txn.amount == Decimal("-42.50")
        assert txn.transaction_type == TransactionType.PURCHASE
        assert txn.status == TransactionStatus.BOOKED
        assert txn.revision == 1

    def test_repr(self) -> None:
        now = datetime.now(UTC)
        txn = Transaction(
            tenant_id=uuid4(),
            provider_key="p",
            external_transaction_id="t3",
            account_id=uuid4(),
            amount=Decimal(50),
            currency_code="EUR",
            occurred_at=now,
            transaction_type=TransactionType.PAYMENT,
        )
        assert "Transaction" in repr(txn)


class TestHoldingModel:
    def test_instantiate(self) -> None:
        now = datetime.now(UTC)
        holding = Holding(
            tenant_id=uuid4(),
            account_id=uuid4(),
            security_id=uuid4(),
            observed_at=now,
            quantity=Decimal(100),
            currency_code="EUR",
            source=HoldingSource.PROVIDER_SYNC,
        )
        assert holding.quantity == Decimal(100)
        assert holding.source == HoldingSource.PROVIDER_SYNC

    def test_repr(self) -> None:
        now = datetime.now(UTC)
        holding = Holding(
            tenant_id=uuid4(),
            account_id=uuid4(),
            security_id=uuid4(),
            observed_at=now,
            quantity=Decimal(10),
            currency_code="USD",
            source=HoldingSource.COMPUTED,
        )
        assert "Holding" in repr(holding)


class TestBalanceModel:
    def test_instantiate(self) -> None:
        now = datetime.now(UTC)
        balance = Balance(
            tenant_id=uuid4(),
            account_id=uuid4(),
            observed_at=now,
            balance_kind=BalanceKind.AVAILABLE,
            amount=Decimal("1500.00"),
            currency_code="EUR",
            source=BalanceSource.PROVIDER_SYNC,
        )
        assert balance.amount == Decimal("1500.00")
        assert balance.balance_kind == BalanceKind.AVAILABLE

    def test_repr(self) -> None:
        now = datetime.now(UTC)
        balance = Balance(
            tenant_id=uuid4(),
            account_id=uuid4(),
            observed_at=now,
            balance_kind=BalanceKind.CURRENT,
            amount=Decimal(100),
            currency_code="EUR",
            source=BalanceSource.MANUAL_ENTRY,
        )
        assert "Balance" in repr(balance)


class TestOutboxMessageModel:
    def test_instantiate(self) -> None:
        msg = OutboxMessage(
            aggregate_id="agg_1",
            aggregate_type="account",
            event_type="account.created",
            payload={"key": "value"},
        )
        assert msg.event_type == "account.created"
        assert msg.payload == {"key": "value"}

    def test_default_status(self) -> None:
        msg = OutboxMessage(
            aggregate_id="a1",
            aggregate_type="txn",
            event_type="transaction.booked",
            payload={},
        )
        assert msg is not None

    def test_repr(self) -> None:
        msg = OutboxMessage(
            aggregate_id="a1",
            aggregate_type="txn",
            event_type="transaction.booked",
            payload={},
        )
        assert "OutboxMessage" in repr(msg)
        assert "transaction.booked" in repr(msg)


class TestSyncRunModel:
    def test_instantiate(self) -> None:
        run = SyncRun(connector="plaid")
        assert run.connector == "plaid"

    def test_completed(self) -> None:
        run = SyncRun(
            connector="teller",
            status=SyncRunStatus.COMPLETED,
            items_processed=42,
        )
        assert run.status == SyncRunStatus.COMPLETED
        assert run.items_processed == 42

    def test_repr(self) -> None:
        run = SyncRun(connector="openbb")
        assert "SyncRun" in repr(run)
        assert "openbb" in repr(run)
