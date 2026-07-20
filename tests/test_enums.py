"""Tests for enum types used in finance-sync domain models."""

from __future__ import annotations

from finance_sync.models.enums import (
    AccountType,
    BalanceKind,
    BalanceSource,
    ConnectorProvider,
    HoldingSource,
    OutboxMessageStatus,
    SecurityType,
    SyncRunStatus,
    TransactionStatus,
    TransactionType,
    UserRole,
)


class TestAccountType:
    def test_members(self) -> None:
        assert AccountType.CHECKING.value == "checking"
        assert AccountType.SAVINGS.value == "savings"
        assert AccountType.BROKERAGE.value == "brokerage"
        assert AccountType.CREDIT.value == "credit"
        assert AccountType.LOAN.value == "loan"
        assert AccountType.INVESTMENT.value == "investment"
        assert AccountType.CASH.value == "cash"
        assert AccountType.OTHER.value == "other"

    def test_str_is_value(self) -> None:
        """Enum stringification matches the DB-stored value."""
        assert str(AccountType.CHECKING) == "checking"


class TestTransactionType:
    def test_members(self) -> None:
        assert TransactionType.TRANSFER.value == "transfer"
        assert TransactionType.PAYMENT.value == "payment"
        assert TransactionType.PURCHASE.value == "purchase"
        assert TransactionType.SALE.value == "sale"
        assert TransactionType.FEE.value == "fee"
        assert TransactionType.INTEREST.value == "interest"
        assert TransactionType.DIVIDEND.value == "dividend"
        assert TransactionType.WITHDRAWAL.value == "withdrawal"
        assert TransactionType.DEPOSIT.value == "deposit"
        assert TransactionType.OTHER.value == "other"


class TestSecurityType:
    def test_members(self) -> None:
        assert SecurityType.STOCK.value == "stock"
        assert SecurityType.ETF.value == "etf"
        assert SecurityType.MUTUAL_FUND.value == "mutual_fund"
        assert SecurityType.BOND.value == "bond"
        assert SecurityType.OPTION.value == "option"
        assert SecurityType.CRYPTO.value == "crypto"
        assert SecurityType.CURRENCY.value == "currency"
        assert SecurityType.OTHER.value == "other"


class TestTransactionStatus:
    def test_members(self) -> None:
        assert TransactionStatus.PENDING.value == "pending"
        assert TransactionStatus.BOOKED.value == "booked"
        assert TransactionStatus.REVERSED.value == "reversed"
        assert TransactionStatus.CANCELLED.value == "cancelled"


class TestBalanceKind:
    def test_members(self) -> None:
        assert BalanceKind.AVAILABLE.value == "available"
        assert BalanceKind.BOOKED.value == "booked"
        assert BalanceKind.CURRENT.value == "current"
        assert BalanceKind.LIMIT.value == "limit"
        assert BalanceKind.CASH.value == "cash"


class TestSyncRunStatus:
    def test_members(self) -> None:
        assert SyncRunStatus.RUNNING.value == "running"
        assert SyncRunStatus.COMPLETED.value == "completed"
        assert SyncRunStatus.FAILED.value == "failed"
        assert SyncRunStatus.CANCELLED.value == "cancelled"


class TestOutboxMessageStatus:
    def test_members(self) -> None:
        assert OutboxMessageStatus.PENDING.value == "pending"
        assert OutboxMessageStatus.SENT.value == "sent"
        assert OutboxMessageStatus.FAILED.value == "failed"


class TestUserRole:
    def test_members(self) -> None:
        assert UserRole.ADMIN.value == "admin"
        assert UserRole.USER.value == "user"
        assert UserRole.READONLY.value == "readonly"
        assert UserRole.VIEWER.value == "viewer"


class TestBalanceSource:
    def test_members(self) -> None:
        assert BalanceSource.PROVIDER_SYNC.value == "provider_sync"
        assert BalanceSource.MANUAL_ENTRY.value == "manual_entry"
        assert BalanceSource.COMPUTED.value == "computed"


class TestHoldingSource:
    def test_members(self) -> None:
        assert HoldingSource.PROVIDER_SYNC.value == "provider_sync"
        assert HoldingSource.COMPUTED.value == "computed"
        assert HoldingSource.MANUAL_ADJUSTMENT.value == "manual_adjustment"


class TestConnectorProvider:
    def test_members(self) -> None:
        assert ConnectorProvider.PLAID.value == "plaid"
        assert ConnectorProvider.TELLER.value == "teller"
        assert ConnectorProvider.OPENBB.value == "openbb"
        assert ConnectorProvider.BUNQ.value == "bunq"
        assert ConnectorProvider.YODLEE.value == "yodlee"
        assert ConnectorProvider.MANUAL.value == "manual"
