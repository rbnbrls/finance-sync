"""Canonical enumerated types for finance-sync domain models.

All enum members use UPPER_CASE names and are stored as their
``.value`` (lower-case) in the database, matching the Phase 1.3
migration defaults.
"""

from __future__ import annotations

from enum import StrEnum


class AccountType(StrEnum):
    """Financial account classification."""

    CHECKING = "checking"
    SAVINGS = "savings"
    BROKERAGE = "brokerage"
    CREDIT = "credit"
    LOAN = "loan"
    INVESTMENT = "investment"
    CASH = "cash"
    OTHER = "other"


class TransactionType(StrEnum):
    """Classification of a financial transaction."""

    TRANSFER = "transfer"
    PAYMENT = "payment"
    PURCHASE = "purchase"
    SALE = "sale"
    FEE = "fee"
    INTEREST = "interest"
    DIVIDEND = "dividend"
    WITHDRAWAL = "withdrawal"
    DEPOSIT = "deposit"
    OTHER = "other"


class TransactionStatus(StrEnum):
    """Lifecycle state of a canonical transaction."""

    PENDING = "pending"
    BOOKED = "booked"
    REVERSED = "reversed"
    CANCELLED = "cancelled"


class SecurityType(StrEnum):
    """Instrument (security) classification."""

    STOCK = "stock"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    BOND = "bond"
    OPTION = "option"
    CRYPTO = "crypto"
    CURRENCY = "currency"
    OTHER = "other"


class BalanceKind(StrEnum):
    """Semantic kind of a balance snapshot."""

    AVAILABLE = "available"
    BOOKED = "booked"
    CURRENT = "current"
    LIMIT = "limit"
    CASH = "cash"


class BalanceSource(StrEnum):
    """Origin of a balance observation."""

    PROVIDER_SYNC = "provider_sync"
    MANUAL_ENTRY = "manual_entry"
    COMPUTED = "computed"


class HoldingSource(StrEnum):
    """Origin of a holding observation."""

    PROVIDER_SYNC = "provider_sync"
    COMPUTED = "computed"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class SyncRunStatus(StrEnum):
    """Lifecycle state of a connector ingestion run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxMessageStatus(StrEnum):
    """Delivery state of a transactional outbox message."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class CostBasisMethod(StrEnum):
    """Cost basis calculation method for tax lots."""

    FIFO = "fifo"
    LIFO = "lifo"
    SPECIFIC_ID = "specific_id"


class WashSaleAdjustmentType(StrEnum):
    """Type of wash sale adjustment applied to a tax lot."""

    LOSS_DISALLOWED = "loss_disallowed"
    BASIS_ADJUSTED = "basis_adjusted"


class ConnectorProvider(StrEnum):
    """Known connector / provider identifiers."""

    PLAID = "plaid"
    TELLER = "teller"
    OPENBB = "openbb"
    BUNQ = "bunq"
    TRADING212 = "trading212"
    YODLEE = "yodlee"
    MANUAL = "manual"


class UserRole(StrEnum):
    """RBAC role for tenant users."""

    ADMIN = "admin"
    USER = "user"
    READONLY = "readonly"
    VIEWER = "viewer"


class WebhookEventType(StrEnum):
    """Event types that can trigger webhook notifications."""

    SYNC_COMPLETED = "sync.completed"
    SYNC_FAILED = "sync.failed"
    TRANSACTION_NEW = "transaction.new"
    PRICE_UPDATED = "price.updated"
    NETWORTH_CHANGED = "networth.changed"


class WebhookDeliveryStatus(StrEnum):
    """Delivery state of a webhook notification."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
