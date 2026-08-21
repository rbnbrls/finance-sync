"""Persistence boundary used by the sync ingestion stages.

The orchestrator owns the UnitOfWork and transaction lifecycle.  This module
owns the dependency that stages use for domain writes, keeping the stages
independent from the orchestration class and making the write surface easy to
replace or test.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol

from finance_sync.models.account import Account
from finance_sync.models.credential import Credential
from finance_sync.models.enums import (
    HoldingSource,
    TransactionStatus,
    TransactionType,
)
from finance_sync.models.holding import Holding
from finance_sync.models.transaction import Transaction
from finance_sync.sync.outbox import (
    outbox_entity_created,
    outbox_entity_updated,
)

if TYPE_CHECKING:
    from finance_sync.connectors.models import (
        CanonicalAccountData,
        CanonicalHoldingData,
        CanonicalTransactionData,
        SecurityReference,
    )
    from finance_sync.db.uow import UnitOfWork


def values_differ(new_val: Any, old_val: Any) -> bool:
    """Compare two field values for change detection.

    Scale-insensitive for Decimals: a ``Numeric(24,8)`` column reads back
    as e.g. ``Decimal('-13.80000000')``, which must compare equal to the
    raw connector ``Decimal('-13.80')``.  Comparing via plain ``!=``
    instead makes every re-sync look "changed", re-emitting
    ``{entity}.updated`` with the same deterministic outbox idempotency
    key until the unique constraint aborts the whole sync run.

    UUID-insensitive: PK/FK columns are ``UUID(as_uuid=True)`` so they
    read back as ``uuid.UUID`` objects, while the sync stages pass ids as
    lowercase hex strings.  ``str()``-normalising both sides keeps
    ``str(uuid)`` equal to the same ``uuid.UUID``.
    """
    if isinstance(new_val, Decimal) or isinstance(old_val, Decimal):
        try:
            return Decimal(str(new_val)) != Decimal(str(old_val))
        except (InvalidOperation, TypeError, ValueError):
            pass
    return str(new_val) != str(old_val)


class PersistenceWriter(Protocol):
    """Minimal write contract required by :class:`SyncPersistence`."""

    async def persist_account(
        self,
        uow: UnitOfWork,
        account: CanonicalAccountData,
        *,
        connection_id: str | None = None,
    ) -> object: ...

    async def persist_transaction(
        self,
        uow: UnitOfWork,
        transaction: CanonicalTransactionData,
        account_id: str,
        *,
        security_id: str | None = None,
        connection_id: str | None = None,
    ) -> object: ...

    async def persist_holding(
        self,
        uow: UnitOfWork,
        holding: CanonicalHoldingData,
        account_id: str,
        security_id: str,
    ) -> object: ...

    async def resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[object | None, str | None]: ...


@dataclass(frozen=True, slots=True)
class PersistenceContext:
    """Immutable context shared by all writes in one sync pipeline."""

    tenant_id: str
    provider_type: str
    connection_id: str | None = None


class SecurityResolver(Protocol):
    """Explicit dependency for transaction and holding security lookup."""

    async def resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[object | None, str | None]: ...


class AccountPersistence:
    """Concrete account upsert/change-detection implementation."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def _connection_owner_id(
        self,
        uow: UnitOfWork,
        connection_id: str | None,
    ) -> str | None:
        if not connection_id:
            return None
        credential = await uow.session.get(Credential, connection_id)
        return credential.owner_user_id if credential is not None else None

    async def persist_account(
        self,
        uow: UnitOfWork,
        account: CanonicalAccountData,
        *,
        connection_id: str | None = None,
    ) -> Account:
        existing = await uow.accounts.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=account.provider_key,
            external_account_id=account.external_account_id,
            connection_id=connection_id,
        )
        fields = (
            "name",
            "account_type",
            "account_subtype",
            "currency_code",
            "current_balance",
            "available_balance",
            "iso_currency_code",
            "provider_metadata",
            "is_active",
        )
        if existing is not None:
            changed: dict[str, object] = {}
            for field in fields:
                value = getattr(account, field, None)
                if value is not None and values_differ(
                    value, getattr(existing, field, None)
                ):
                    setattr(existing, field, value)
                    changed[field] = value
            if changed:
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    tenant_id=self._tenant_id,
                    entity_type="account",
                    entity_id=str(existing.id),
                    changed_fields=changed,
                    provider_key=account.provider_key,
                )
            return existing

        from uuid import uuid4

        entity = Account(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=account.provider_key,
            connection_id=connection_id,
            owner_user_id=await self._connection_owner_id(uow, connection_id),
            external_account_id=account.external_account_id,
            name=account.name,
            account_type=account.account_type,
            account_subtype=account.account_subtype,
            currency_code=account.currency_code,
            current_balance=account.current_balance,
            available_balance=account.available_balance,
            iso_currency_code=account.iso_currency_code,
            provider_metadata=account.provider_metadata,
            is_active=account.is_active,
        )
        uow.session.add(entity)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            tenant_id=self._tenant_id,
            entity_type="account",
            entity_id=str(entity.id),
            entity_data={
                "provider_key": account.provider_key,
                "external_account_id": account.external_account_id,
                "name": account.name,
            },
            provider_key=account.provider_key,
        )
        return entity


class TransactionPersistence:
    """Concrete transaction upsert/change-detection implementation."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def persist_transaction(
        self,
        uow: UnitOfWork,
        transaction: CanonicalTransactionData,
        account_id: str,
        *,
        security_id: str | None = None,
        connection_id: str | None = None,
    ) -> Transaction:
        existing = await uow.transactions.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=transaction.provider_key,
            external_transaction_id=transaction.external_transaction_id,
            connection_id=connection_id,
        )
        fields = (
            "amount",
            "currency_code",
            "occurred_at",
            "booked_at",
            "transaction_type",
            "description",
            "quantity",
            "unit_price",
            "fee_amount",
            "fee_currency_code",
            "status",
            "amount_in_base",
            "base_currency_code",
            "fx_rate",
            "provider_fingerprint",
        )
        if existing is not None:
            changed: dict[str, object] = {}
            for field in fields:
                value = getattr(transaction, field, None)
                if value is not None and values_differ(
                    value, getattr(existing, field, None)
                ):
                    setattr(existing, field, value)
                    changed[field] = value
            if security_id is not None and values_differ(
                security_id, existing.security_id
            ):
                existing.security_id = security_id
                changed["security_id"] = security_id
            if changed:
                existing.revision = (existing.revision or 0) + 1
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    tenant_id=self._tenant_id,
                    entity_type="transaction",
                    entity_id=str(existing.id),
                    changed_fields=changed,
                    provider_key=transaction.provider_key,
                )
            return existing

        from uuid import uuid4

        transaction_type = (
            TransactionType(transaction.transaction_type)
            if transaction.transaction_type
            in (TransactionType.__members__.values())
            else TransactionType.OTHER
        )
        transaction_status = (
            TransactionStatus(transaction.status)
            if transaction.status in TransactionStatus.__members__.values()
            else TransactionStatus.PENDING
        )
        entity = Transaction(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=transaction.provider_key,
            connection_id=connection_id,
            external_transaction_id=transaction.external_transaction_id,
            account_id=account_id,
            security_id=security_id,
            amount=Decimal(str(transaction.amount)),
            currency_code=transaction.currency_code,
            amount_in_base=(
                Decimal(str(transaction.amount_in_base))
                if transaction.amount_in_base is not None
                else None
            ),
            base_currency_code=transaction.base_currency_code,
            fx_rate=(
                Decimal(str(transaction.fx_rate))
                if transaction.fx_rate is not None
                else None
            ),
            occurred_at=transaction.occurred_at,
            booked_at=transaction.booked_at,
            transaction_type=transaction_type,
            description=transaction.description,
            quantity=transaction.quantity,
            unit_price=transaction.unit_price,
            fee_amount=transaction.fee_amount,
            fee_currency_code=transaction.fee_currency_code,
            status=transaction_status,
            provider_fingerprint=transaction.provider_fingerprint,
            revision=1,
        )
        uow.session.add(entity)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            tenant_id=self._tenant_id,
            entity_type="transaction",
            entity_id=str(entity.id),
            entity_data={
                "provider_key": transaction.provider_key,
                "external_transaction_id": transaction.external_transaction_id,
                "amount": str(transaction.amount),
                "currency_code": transaction.currency_code,
            },
            provider_key=transaction.provider_key,
        )
        return entity


class HoldingPersistence:
    """Concrete time-versioned holding snapshot persistence."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def persist_holding(
        self,
        uow: UnitOfWork,
        holding: CanonicalHoldingData,
        account_id: str,
        security_id: str,
    ) -> Holding:
        try:
            source = HoldingSource(holding.source)
        except ValueError:
            source = HoldingSource.PROVIDER_SYNC
        existing = await uow.holdings.get_by_snapshot(
            self._tenant_id,
            account_id,
            security_id,
            holding.observed_at,
            source.value,
        )
        values = {
            "quantity": Decimal(str(holding.quantity)),
            "cost_basis": (
                Decimal(str(holding.cost_basis))
                if holding.cost_basis is not None
                else None
            ),
            "cost_basis_currency": holding.cost_basis_currency,
            "market_value": (
                Decimal(str(holding.market_value))
                if holding.market_value is not None
                else None
            ),
            "currency_code": holding.currency_code,
            "price": (
                Decimal(str(holding.price))
                if holding.price is not None
                else None
            ),
            "price_currency": holding.price_currency,
        }
        if existing is not None:
            changed = False
            for field, value in values.items():
                if values_differ(value, getattr(existing, field)):
                    setattr(existing, field, value)
                    changed = True
            if changed:
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    tenant_id=self._tenant_id,
                    entity_type="holding",
                    entity_id=str(existing.id),
                    changed_fields={"snapshot_updated": True},
                    provider_key=holding.provider_key,
                )
            return existing
        from uuid import uuid4

        entity = Holding(
            id=uuid4(),
            tenant_id=self._tenant_id,
            account_id=account_id,
            security_id=security_id,
            observed_at=holding.observed_at,
            source=source,
            **values,
        )
        uow.session.add(entity)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            tenant_id=self._tenant_id,
            entity_type="holding",
            entity_id=str(entity.id),
            entity_data={"observed_at": holding.observed_at.isoformat()},
            provider_key=holding.provider_key,
        )
        return entity


class SyncPersistence:
    """Explicit, commit-free persistence adapter for sync stages.

    ``SyncPersistence`` deliberately does not expose ``commit`` or ``rollback``.
    Those operations remain the responsibility of the caller-owned UoW.
    """

    def __init__(
        self,
        writer: PersistenceWriter,
        *,
        context: PersistenceContext | None = None,
    ) -> None:
        self._writer = writer
        self.context = context
        self._account_persistence = AccountPersistence(
            context.tenant_id if context is not None else ""
        )
        self._transaction_persistence = TransactionPersistence(
            context.tenant_id if context is not None else ""
        )
        self._holding_persistence = HoldingPersistence(
            context.tenant_id if context is not None else ""
        )

    async def persist_account(
        self,
        uow: UnitOfWork,
        account: CanonicalAccountData,
        *,
        connection_id: str | None = None,
    ) -> object:
        if self.context is None:
            return await self._writer.persist_account(
                uow, account, connection_id=connection_id
            )
        return await self._account_persistence.persist_account(
            uow, account, connection_id=connection_id
        )

    async def persist_transaction(
        self,
        uow: UnitOfWork,
        transaction: CanonicalTransactionData,
        account_id: str,
        *,
        security_id: str | None = None,
        connection_id: str | None = None,
    ) -> object:
        if self.context is None:
            return await self._writer.persist_transaction(
                uow,
                transaction,
                account_id,
                security_id=security_id,
                connection_id=connection_id,
            )
        return await self._transaction_persistence.persist_transaction(
            uow,
            transaction,
            account_id,
            security_id=security_id,
            connection_id=connection_id,
        )

    async def persist_holding(
        self,
        uow: UnitOfWork,
        holding: CanonicalHoldingData,
        account_id: str,
        security_id: str,
    ) -> object:
        if self.context is None:
            return await self._writer.persist_holding(
                uow, holding, account_id, security_id
            )
        return await self._holding_persistence.persist_holding(
            uow, holding, account_id, security_id
        )

    async def resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[object | None, str | None]:
        return await self._writer.resolve_security_reference(
            uow, provider_key, reference
        )
