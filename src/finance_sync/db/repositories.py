# pyright: basic
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
    TaxLot,
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


class TaxLotRepository(Repository[TaxLot]):
    model_class = TaxLot

    async def find_open_lots(
        self,
        tenant_id: str,
        account_id: str,
        security_id: str,
    ) -> list[TaxLot]:
        """Return all open (unclosed) tax lots for an account+security, ordered by acquisition date.

        Used by the cost-basis matching engine.
        """
        return await self.list(
            TaxLot.tenant_id == tenant_id,  # type: ignore[attr-defined]
            TaxLot.account_id == account_id,  # type: ignore[attr-defined]
            TaxLot.security_id == security_id,  # type: ignore[attr-defined]
            TaxLot.closed_at.is_(None),  # type: ignore[attr-defined]
            order_by=TaxLot.acquired_at.asc(),  # type: ignore[attr-defined]
        )

    async def find_lots_for_transaction(
        self,
        tenant_id: str,
        transaction_id: str,
    ) -> list[TaxLot]:
        """Find all tax lots linked to a specific transaction (purchase or sale)."""
        return await self.list(
            TaxLot.tenant_id == tenant_id,  # type: ignore[attr-defined]
            (
                (TaxLot.purchase_transaction_id == transaction_id)  # type: ignore[attr-defined]
                | (TaxLot.sale_transaction_id == transaction_id)  # type: ignore[attr-defined]
            ),
        )


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


class ExportRunRepository(Repository[ExportRun]):  # type: ignore[reportInvalidTypeArguments]
    model_class = ExportRun  # type: ignore[reportAssignmentType]


class ActualBudgetAccountMappingRepository(
    Repository[ActualBudgetAccountMapping]  # type: ignore[reportInvalidTypeArguments]
):
    model_class = ActualBudgetAccountMapping  # type: ignore[reportAssignmentType]


class WebhookRepository(Repository[Webhook]):
    model_class = Webhook


class WebhookDeliveryLogRepository(Repository[WebhookDeliveryLog]):
    model_class = WebhookDeliveryLog
