# pyright: basic
"""Concrete repository implementations for finance-sync models.

Each repository inherits from ``Repository[T]`` and sets ``model_class``
to the corresponding SQLAlchemy model.
"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_sync.db.repository import Repository
from finance_sync.models import (
    Account,
    ActualBudgetAccountMapping,
    Balance,
    EnrichmentFreshness,
    ExportRun,
    FxRate,
    Holding,
    OutboxMessage,
    ReconciliationResult,
    ReconciliationRun,
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

    async def find_duplicate_candidates(
        self,
        tenant_id: str,
        *,
        account_ids: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        threshold_hours: int = 48,
    ) -> list[tuple[Transaction, Transaction]]:
        """Find pairs of transactions that may be duplicates within an account.

        A candidate pair is two transactions in the same account with
        identical amounts and close occurrence dates (within
        *threshold_hours*) but different external IDs or provider keys.

        Returns a list of (tx_a, tx_b) tuples, ordered by descending
        amount magnitude so the most suspicious pairs come first.
        """
        from collections import defaultdict

        import structlog

        log = structlog.get_logger("finance_sync.repo.transactions")
        conditions = [Transaction.tenant_id == tenant_id]  # type: ignore[attr-defined]

        if account_ids:
            conditions.append(
                Transaction.account_id.in_(account_ids)  # type: ignore[attr-defined]
            )
        if date_from is not None:
            conditions.append(
                Transaction.occurred_at >= date_from  # type: ignore[attr-defined]
            )
        if date_to is not None:
            conditions.append(
                Transaction.occurred_at <= date_to  # type: ignore[attr-defined]
            )

        all_txns = await self.list(*conditions, limit=5000)
        log.debug(
            "duplicate_candidate_scan",
            total=len(all_txns),
            account_ids=account_ids,
        )

        # Group by (account_id, amount) — same amount + same account
        groups: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
        for t in all_txns:
            key = (str(t.account_id), str(t.amount))
            groups[key].append(t)

        pairs: list[tuple[Transaction, Transaction]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            # Sort by occurred_at
            group.sort(
                key=lambda t: t.occurred_at or datetime(1970, 1, 1, tzinfo=UTC)
            )
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    # Skip if same provider key AND same external ID
                    if (
                        a.provider_key == b.provider_key
                        and a.external_transaction_id
                        == b.external_transaction_id
                    ):
                        continue
                    # Check time proximity
                    t_a = a.occurred_at
                    t_b = b.occurred_at
                    if t_a is None or t_b is None:
                        continue
                    diff_hours = abs((t_a - t_b).total_seconds()) / 3600
                    if diff_hours <= threshold_hours:
                        pairs.append((a, b))

        # Sort by absolute amount descending (biggest duplicates first)
        pairs.sort(key=lambda p: abs(p[0].amount or 0), reverse=True)
        return pairs

    async def get_providers_for_account(
        self,
        tenant_id: str,
        account_id: str,
    ) -> list[str]:
        """Return distinct provider_keys with transactions for this account."""
        from sqlalchemy import select

        stmt = (
            select(Transaction.provider_key)  # type: ignore[attr-defined]
            .where(
                Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
                Transaction.account_id == account_id,  # type: ignore[attr-defined]
            )
            .distinct()
        )
        results = await self._session.execute(stmt)
        return list(results.scalars().all())

    async def get_transaction_date_range(
        self,
        tenant_id: str,
        *,
        account_id: str | None = None,
        provider_key: str | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        """Return the (earliest, latest) occurrence dates for transactions."""
        from sqlalchemy import func, select

        conditions = [Transaction.tenant_id == tenant_id]  # type: ignore[attr-defined]
        if account_id is not None:
            conditions.append(
                Transaction.account_id == account_id  # type: ignore[attr-defined]
            )
        if provider_key is not None:
            conditions.append(
                Transaction.provider_key == provider_key  # type: ignore[attr-defined]
            )

        stmt = select(
            func.min(Transaction.occurred_at),  # type: ignore[attr-defined]
            func.max(Transaction.occurred_at),  # type: ignore[attr-defined]
        ).where(*conditions)
        result = await self._session.execute(stmt)
        row = result.one()
        return (row[0], row[1])


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


class ReconciliationRunRepository(Repository[ReconciliationRun]):
    """Repository for reconciliation run tracking."""

    model_class = ReconciliationRun


class ReconciliationResultRepository(Repository[ReconciliationResult]):
    """Repository for reconciliation findings."""

    model_class = ReconciliationResult


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


class FxRateRepository(Repository[FxRate]):
    model_class = FxRate
