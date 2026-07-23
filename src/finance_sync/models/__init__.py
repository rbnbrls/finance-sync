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

# Core models — imported eagerly (no circular deps with finance_sync.models)
from finance_sync.models.account import Account
from finance_sync.models.api_key import ApiKey
from finance_sync.models.balance import Balance
from finance_sync.models.card_transaction import CardTransaction
from finance_sync.models.credential import Credential
from finance_sync.models.enrichment_freshness import EnrichmentFreshness
from finance_sync.models.detected_subscription import DetectedSubscription
from finance_sync.models.enums import (
    AccountType,
    BalanceKind,
    BalanceSource,
    CardAuthorizationType,
    ConnectorProvider,
    CostBasisMethod,
    DetectionMethod,
    HoldingSource,
    OutboxMessageStatus,
    ReconciliationResultKind,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    ScheduleFrequency,
    ScheduleStatus,
    SecurityType,
    SubscriptionConfidence,
    SubscriptionStatus,
    SyncRunStatus,
    TransactionStatus,
    TransactionType,
    UserRole,
    WashSaleAdjustmentType,
    WebhookDeliveryStatus,
    WebhookEventType,
)
from finance_sync.models.fundamental_observation import FundamentalObservation
from finance_sync.models.fx_rate import FxRate
from finance_sync.models.holding import Holding
from finance_sync.models.mixins import TenantAwareMixin, TimestampMixin
from finance_sync.models.outbox import OutboxMessage
from finance_sync.models.reconciliation import (
    ReconciliationResult,
    ReconciliationRun,
)
from finance_sync.models.resolution_audit_log import ResolutionAuditLog
from finance_sync.models.scheduled_payment import ScheduledPayment
from finance_sync.models.security import Security
from finance_sync.models.security_listing import SecurityListing
from finance_sync.models.security_metadata_observation import (
    SecurityMetadataObservation,
)
from finance_sync.models.security_price import SecurityPrice
from finance_sync.models.sync_run import SyncRun
from finance_sync.models.tax_lot import TaxLot
from finance_sync.models.tenant import Tenant
from finance_sync.models.transaction import Transaction
from finance_sync.models.unresolved_security import UnresolvedSecurity
from finance_sync.models.user import User
from finance_sync.models.webhook import Webhook, WebhookDeliveryLog

# ── Lazy exporter model registration ─────────────────────────────────
# These are imported lazily so that finance_sync.exporter.exporter
# (which imports finance_sync.models) does not create a circular dep.
# They are still registered on Base.metadata for Alembic autogenerate;
# call ensure_exporter_models_loaded() at startup or in Alembic env.py.

_actual_budget_account_mapping: type | None = None
_export_run: type | None = None
_export_delivery: type | None = None


def ensure_exporter_models_loaded() -> None:
    """Eagerly import exporter ORM models so they register on
    ``Base.metadata`` for Alembic autogenerate detection.

    Safe to call multiple times.
    """
    global _actual_budget_account_mapping, _export_run, _export_delivery
    if _actual_budget_account_mapping is None:
        from finance_sync.exporter.actual_budget.models import (
            ActualBudgetAccountMapping,
            ExportDelivery,
        )
        from finance_sync.exporter.models import ExportRun

        _actual_budget_account_mapping = ActualBudgetAccountMapping
        _export_run = ExportRun
        _export_delivery = ExportDelivery


def __getattr__(name: str) -> object:
    """Support ``from finance_sync.models import ActualBudgetAccountMapping``,
    ``ExportDelivery`` and ``ExportRun`` even though they are loaded lazily.

    This makes the API transparent to callers — they don't need to know
    whether a model is loaded eagerly or lazily.
    """
    if name == "ActualBudgetAccountMapping":
        ensure_exporter_models_loaded()
        if _actual_budget_account_mapping is not None:
            return _actual_budget_account_mapping
    if name == "ExportRun":
        ensure_exporter_models_loaded()
        if _export_run is not None:
            return _export_run
    if name == "ExportDelivery":
        ensure_exporter_models_loaded()
        if _export_delivery is not None:
            return _export_delivery
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    # Models
    "Account",
    # Enums
    "AccountType",
    "ActualBudgetAccountMapping",
    "ApiKey",
    "Balance",
    "BalanceKind",
    "BalanceSource",
    "CardAuthorizationType",
    "CardTransaction",
    "ConnectorProvider",
    "CostBasisMethod",
    "Credential",
    "DetectedSubscription",
    "DetectionMethod",
    "EnrichmentFreshness",
    "ExportDelivery",
    "ExportRun",
    "FundamentalObservation",
    "FxRate",
    "Holding",
    "HoldingSource",
    "OutboxMessage",
    "OutboxMessageStatus",
    "ReconciliationResult",
    "ReconciliationResultKind",
    "ReconciliationRun",
    "ReconciliationRunStatus",
    "ReconciliationSeverity",
    "ResolutionAuditLog",
    "ScheduleFrequency",
    "ScheduleStatus",
    "ScheduledPayment",
    "Security",
    "SecurityListing",
    "SecurityMetadataObservation",
    "SecurityPrice",
    "SecurityType",
    "SubscriptionConfidence",
    "SubscriptionStatus",
    "SyncRun",
    "SyncRunStatus",
    "TaxLot",
    "Tenant",
    # Mixins
    "TenantAwareMixin",
    "TimestampMixin",
    "Transaction",
    "TransactionStatus",
    "TransactionType",
    "UnresolvedSecurity",
    "User",
    "UserRole",
    "WashSaleAdjustmentType",
    "Webhook",
    "WebhookDeliveryLog",
    "WebhookDeliveryStatus",
    "WebhookEventType",
]
