"""Concrete repository implementations for finance-sync models.

Each repository inherits from ``Repository[T]`` and sets ``model_class``
to the corresponding SQLAlchemy model.
"""

from __future__ import annotations

from finance_sync.db.repository import Repository
from finance_sync.models import (
    Account,
    ActualBudgetAccountMapping,
    Balance,
    EnrichmentFreshness,
    ExportRun,
    Holding,
    OutboxMessage,
    ResolutionAuditLog,
    Security,
    SecurityListing,
    SecurityPrice,
    SyncRun,
    Tenant,
    Transaction,
    UnresolvedSecurity,
    User,
    Webhook,
    WebhookDeliveryLog,
)


class TenantRepository(Repository[Tenant]):
    model_class = Tenant


class UserRepository(Repository[User]):
    model_class = User


class AccountRepository(Repository[Account]):
    model_class = Account

    async def get_by_external_id(
        self,
        tenant_id: str,
        provider_key: str,
        external_account_id: str,
    ) -> Account | None:
        """Find an account by its provider-scoped external ID."""
        results = await self.list(
            Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Account.provider_key == provider_key,  # type: ignore[attr-defined]
            Account.external_account_id == external_account_id,  # type: ignore[attr-defined]
            limit=1,
        )
        return results[0] if results else None


class SecurityRepository(Repository[Security]):
    model_class = Security


class SecurityListingRepository(Repository[SecurityListing]):
    model_class = SecurityListing


class SecurityPriceRepository(Repository[SecurityPrice]):
    model_class = SecurityPrice


class EnrichmentFreshnessRepository(Repository[EnrichmentFreshness]):
    model_class = EnrichmentFreshness


class TransactionRepository(Repository[Transaction]):
    model_class = Transaction

    async def get_by_external_id(
        self,
        tenant_id: str,
        provider_key: str,
        external_transaction_id: str,
    ) -> Transaction | None:
        """Find a transaction by its provider-scoped external ID."""
        results = await self.list(
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Transaction.provider_key == provider_key,  # type: ignore[attr-defined]
            Transaction.external_transaction_id == external_transaction_id,  # type: ignore[attr-defined]
            limit=1,
        )
        return results[0] if results else None


class HoldingRepository(Repository[Holding]):
    model_class = Holding


class BalanceRepository(Repository[Balance]):
    model_class = Balance


class OutboxMessageRepository(Repository[OutboxMessage]):
    model_class = OutboxMessage


class SyncRunRepository(Repository[SyncRun]):
    model_class = SyncRun


class UnresolvedSecurityRepository(Repository[UnresolvedSecurity]):
    model_class = UnresolvedSecurity


class ResolutionAuditLogRepository(Repository[ResolutionAuditLog]):
    model_class = ResolutionAuditLog


class ExportRunRepository(Repository[ExportRun]):
    model_class = ExportRun


class ActualBudgetAccountMappingRepository(
    Repository[ActualBudgetAccountMapping]
):
    model_class = ActualBudgetAccountMapping


class WebhookRepository(Repository[Webhook]):
    model_class = Webhook


class WebhookDeliveryLogRepository(Repository[WebhookDeliveryLog]):
    model_class = WebhookDeliveryLog
