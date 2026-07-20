"""Canonical SQLAlchemy models.

Importing this module registers all models on ``Base.metadata`` so that
Alembic can detect them via ``--autogenerate``.

Usage
-----
::

    from finance_sync.models import Tenant, User, Account, ...
    from finance_sync.models.enums import AccountType, TransactionType, ...
"""

from __future__ import annotations

# Exporter ORM models — imported here so Alembic autogenerate can find them
from finance_sync.exporter.models import (
    ActualBudgetAccountMapping,
    ExportRun,
)
from finance_sync.models.account import Account
from finance_sync.models.api_key import ApiKey
from finance_sync.models.balance import Balance
from finance_sync.models.credential import Credential
from finance_sync.models.enrichment_freshness import EnrichmentFreshness
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
from finance_sync.models.holding import Holding
from finance_sync.models.mixins import TenantAwareMixin, TimestampMixin
from finance_sync.models.outbox import OutboxMessage
from finance_sync.models.resolution_audit_log import ResolutionAuditLog
from finance_sync.models.security import Security
from finance_sync.models.security_listing import SecurityListing
from finance_sync.models.security_price import SecurityPrice
from finance_sync.models.sync_run import SyncRun
from finance_sync.models.tenant import Tenant
from finance_sync.models.transaction import Transaction
from finance_sync.models.unresolved_security import UnresolvedSecurity
from finance_sync.models.user import User

__all__ = [
    # Models
    "Account",
    "ActualBudgetAccountMapping",
    "ApiKey",
    "Balance",
    "Credential",
    "EnrichmentFreshness",
    "ExportRun",
    "Holding",
    "OutboxMessage",
    "ResolutionAuditLog",
    "Security",
    "SecurityListing",
    "SecurityPrice",
    "SyncRun",
    "Tenant",
    "Transaction",
    "UnresolvedSecurity",
    "User",
    # Enums
    "AccountType",
    "BalanceKind",
    "BalanceSource",
    "ConnectorProvider",
    "HoldingSource",
    "OutboxMessageStatus",
    "SecurityType",
    "SyncRunStatus",
    "TransactionStatus",
    "TransactionType",
    "UserRole",
    # Mixins
    "TenantAwareMixin",
    "TimestampMixin",
]
