"""Persistence boundary used by the sync ingestion stages.

The orchestrator owns the UnitOfWork and transaction lifecycle.  This module
owns the dependency that stages use for domain writes, keeping the stages
independent from the orchestration class and making the write surface easy to
replace or test.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select

from finance_sync.models.account import Account
from finance_sync.models.balance import Balance
from finance_sync.models.card_transaction import CardTransaction
from finance_sync.models.credential import Credential
from finance_sync.models.enums import (
    CardAuthorizationType,
    HoldingSource,
    ScheduleFrequency,
    ScheduleStatus,
    SecurityType,
    TransactionStatus,
    TransactionType,
)
from finance_sync.models.holding import Holding
from finance_sync.models.scheduled_payment import ScheduledPayment
from finance_sync.models.security import Security
from finance_sync.models.spending import (
    TransactionAnnotation,
    TransactionSourceReference,
    TransactionSplit,
)
from finance_sync.models.transaction import Transaction
from finance_sync.models.transaction_event import TransactionLifecycleEvent
from finance_sync.models.unresolved_security import UnresolvedSecurity
from finance_sync.sync.outbox import (
    outbox_entity_created,
    outbox_entity_updated,
)

if TYPE_CHECKING:
    from finance_sync.connectors.models import (
        CanonicalAccountData,
        CanonicalCardTransactionData,
        CanonicalHoldingData,
        CanonicalScheduledPaymentData,
        CanonicalTransactionData,
        SecurityReference,
    )
    from finance_sync.db.uow import UnitOfWork
    from finance_sync.sync.upserts import UpsertResult


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


def _transaction_extension_values(transaction: Any) -> dict[str, Any]:
    """Return optional spending fields without breaking old connectors."""
    values: dict[str, Any] = {}
    for field in (
        "provider_metadata_contract",
        "merchant_name",
        "merchant_id",
        "merchant_city",
        "merchant_country",
        "counterparty_name",
        "counterparty_account_reference",
        "merchant_category_code",
        "original_type",
        "original_status",
        "authorization_status",
        "settlement_status",
        "source_record_hash",
        "cashflow_bucket",
        "classification_source",
        "classification_override",
        "gross_amount",
        "gross_currency_code",
        "net_amount",
        "net_currency_code",
        "tax_amount",
        "tax_currency_code",
        "refund_amount",
        "refund_currency_code",
    ):
        value = getattr(transaction, field, None)
        if value is not None and hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if value is not None:
            values[field] = value
    suggestion = getattr(transaction, "cashflow_suggestion", None)
    if suggestion is not None:
        values["cashflow_suggestion"] = suggestion.model_dump(mode="json")
    return values


def _add_lifecycle_event(
    uow: Any,
    *,
    tenant_id: str,
    transaction: Transaction,
    event_type: str,
    payload: dict[str, Any],
    provenance: str = "provider_sync",
    actor: str | None = None,
) -> None:
    """Append a deterministic event; the DB constraint makes retries safe."""
    provider_key = getattr(transaction, "provider_key", "unknown")
    external_id = getattr(
        transaction, "external_transaction_id", transaction.id
    )
    uow.session.add(
        TransactionLifecycleEvent(
            tenant_id=tenant_id,
            transaction_id=str(transaction.id),
            event_type=event_type,
            idempotency_key=(
                f"{provider_key}:{external_id}:"
                f"{event_type}:{transaction.revision}"
            ),
            payload=payload,
            provenance=provenance,
            actor=actor,
            source_revision=transaction.revision,
        )
    )


def _add_transaction_details(
    uow: Any, transaction: Transaction, source: Any
) -> None:
    """Persist non-destructive source relations and annotations on create."""
    for reference in getattr(source, "source_references", ()):
        uow.session.add(
            TransactionSourceReference(
                tenant_id=transaction.tenant_id,
                transaction_id=transaction.id,
                object_type=reference.object_type,
                external_ids=list(reference.external_ids),
                provider_revisions=list(reference.provider_revisions),
            )
        )
    for split in getattr(source, "splits", ()):
        suggestion = getattr(split, "category_suggestion", None)
        uow.session.add(
            TransactionSplit(
                tenant_id=transaction.tenant_id,
                transaction_id=transaction.id,
                amount=Decimal(str(split.amount)),
                currency_code=split.currency_code,
                percentage=split.percentage,
                category_suggestion=(
                    suggestion.model_dump(mode="json")
                    if suggestion is not None
                    and hasattr(suggestion, "model_dump")
                    else suggestion
                ),
                destination=split.destination,
                provenance=split.provenance,
            )
        )
    for annotation in getattr(source, "annotations", ()):
        uow.session.add(
            TransactionAnnotation(
                tenant_id=transaction.tenant_id,
                transaction_id=transaction.id,
                annotation_type=annotation.annotation_type,
                content_hash=annotation.content_hash,
                mime_type=annotation.mime_type,
                safe_reference=annotation.safe_reference,
                owner=annotation.owner,
                retention_until=annotation.retention_until,
                destination_reference=annotation.destination_reference,
            )
        )


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
        # Older Trading212 API responses sometimes failed the account-info
        # call and the connector used the literal fallback ``trading212``.
        # When the real broker id becomes available, adopt that legacy row
        # instead of creating a second local account.  This preserves any
        # holdings already imported during the degraded run.
        if (
            existing is None
            and account.provider_key == "trading212"
            and account.external_account_id != "trading212"
        ):
            legacy = await uow.session.scalar(
                select(Account).where(
                    Account.tenant_id == self._tenant_id,
                    Account.provider_key == "trading212",
                    Account.connection_id == connection_id,
                    Account.external_account_id == "trading212",
                )
            )
            if legacy is not None:
                legacy.external_account_id = account.external_account_id
                existing = legacy
        fields = (
            "name",
            "account_type",
            "account_subtype",
            "currency_code",
            "current_balance",
            "available_balance",
            "net_asset_value",
            "iso_currency_code",
            "provider_metadata",
            "capabilities",
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
                    deduplicate=True,
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
            net_asset_value=account.net_asset_value,
            iso_currency_code=account.iso_currency_code,
            provider_metadata=account.provider_metadata,
            capabilities=getattr(account, "capabilities", None),
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

    async def persist_cash_balances(
        self, uow: UnitOfWork, account_id: str, account: CanonicalAccountData
    ) -> None:
        """Upsert provider cash snapshots without collapsing currencies."""
        for snapshot in account.cash_balances:
            currency = snapshot.currency_code.upper()
            existing = await uow.session.scalar(
                select(Balance).where(
                    Balance.account_id == account_id,
                    Balance.observed_at == snapshot.observed_at,
                    Balance.balance_kind == snapshot.balance_kind,
                    Balance.currency_code == currency,
                )
            )
            if existing is None:
                uow.session.add(
                    Balance(
                        tenant_id=self._tenant_id,
                        account_id=account_id,
                        observed_at=snapshot.observed_at,
                        balance_kind=snapshot.balance_kind,
                        amount=snapshot.amount,
                        currency_code=currency,
                        source="provider_sync",
                    )
                )
            else:
                existing.amount = snapshot.amount


class TransactionPersistence:
    """Concrete transaction upsert/change-detection implementation."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id
        self._last_upsert_outcome: dict[str, int] = {}

    @property
    def last_upsert_outcome(self) -> dict[str, int]:
        """Return exact counters from the most recent transaction write."""
        return dict(self._last_upsert_outcome)

    async def apply_destination_enrichment(
        self,
        uow: UnitOfWork,
        transaction_id: str,
        enrichment: dict[str, Any],
    ) -> Transaction | None:
        """Apply destination-owned enrichment without replacing user state."""
        entity = await uow.session.get(Transaction, transaction_id)
        if entity is None or str(entity.tenant_id) != self._tenant_id:
            return None
        protected = {
            "classification_override",
            "category",
            "category_assignment",
            "splits",
            "events",
            "notes",
        }
        changed: dict[str, object] = {}
        for field, value in enrichment.items():
            if (
                field in protected
                or value is None
                or not hasattr(entity, field)
            ):
                continue
            if values_differ(value, getattr(entity, field, None)):
                setattr(entity, field, value)
                changed[field] = value
        if changed:
            entity.revision = (entity.revision or 0) + 1
            _add_lifecycle_event(
                uow,
                tenant_id=self._tenant_id,
                transaction=entity,
                event_type="update",
                payload={"changed_fields": sorted(changed)},
                provenance="destination_enrichment",
            )
            await uow.session.flush()
        return entity

    async def tombstone_transaction(
        self,
        uow: UnitOfWork,
        transaction_id: str,
        *,
        actor: str | None = None,
    ) -> Transaction | None:
        """Soft-delete a transaction while retaining its source history."""
        entity = await uow.session.get(Transaction, transaction_id)
        if entity is None or str(entity.tenant_id) != self._tenant_id:
            return None
        if entity.tombstoned_at is not None:
            return entity
        entity.tombstoned_at = datetime.now(UTC)
        entity.status = TransactionStatus.CANCELLED
        entity.revision = (entity.revision or 0) + 1
        _add_lifecycle_event(
            uow,
            tenant_id=self._tenant_id,
            transaction=entity,
            event_type="tombstone",
            payload={"reason": "explicit_user_delete"},
            provenance="user_override",
            actor=actor,
        )
        await uow.session.flush()
        return entity

    async def persist_transactions_batch(
        self,
        uow: UnitOfWork,
        transactions: Sequence[CanonicalTransactionData],
        account_id: str,
        *,
        security_ids: Sequence[str | None] | None = None,
        connection_id: str | None = None,
    ) -> int:
        """Bulk-upsert many transactions for one account in a single call.

        Uses the PostgreSQL ``INSERT .. ON CONFLICT DO UPDATE`` path when
        the session is bound to PostgreSQL (one round-trip, database-level
        idempotency); falls back to the per-row :meth:`persist_transaction`
        loop on other dialects (SQLite unit tests, mock sessions).
        """
        from finance_sync.sync.upserts import (
            UpsertResult,
            _is_postgresql,  # pyright: ignore[reportPrivateUsage]
            bulk_upsert_transactions,
        )

        resolved_security_ids: list[str | None] = (
            list(security_ids)
            if security_ids is not None
            else [None] * len(transactions)
        )
        if len(resolved_security_ids) != len(transactions):
            msg = "security_ids length must match transactions length"
            raise ValueError(msg)
        self._last_upsert_outcome = {}

        rows: list[dict[str, Any]] = []
        for index, transaction in enumerate(transactions):
            transaction_type = (
                TransactionType(transaction.transaction_type)
                if transaction.transaction_type
                in TransactionType.__members__.values()
                else TransactionType.OTHER
            )
            transaction_status = (
                TransactionStatus(transaction.status)
                if transaction.status in TransactionStatus.__members__.values()
                else TransactionStatus.PENDING
            )
            from uuid import uuid4

            rows.append(
                {
                    "id": uuid4(),
                    "tenant_id": self._tenant_id,
                    "provider_key": transaction.provider_key,
                    "connection_id": connection_id,
                    "external_transaction_id": (
                        transaction.external_transaction_id
                    ),
                    "account_id": account_id,
                    "security_id": resolved_security_ids[index],
                    "amount": Decimal(str(transaction.amount)),
                    "currency_code": transaction.currency_code,
                    "amount_in_base": (
                        Decimal(str(transaction.amount_in_base))
                        if transaction.amount_in_base is not None
                        else None
                    ),
                    "base_currency_code": transaction.base_currency_code,
                    "fx_rate": (
                        Decimal(str(transaction.fx_rate))
                        if transaction.fx_rate is not None
                        else None
                    ),
                    "occurred_at": transaction.occurred_at,
                    "booked_at": transaction.booked_at,
                    "transaction_type": transaction_type,
                    "description": transaction.description,
                    "quantity": transaction.quantity,
                    "unit_price": transaction.unit_price,
                    "fee_amount": transaction.fee_amount,
                    "fee_currency_code": transaction.fee_currency_code,
                    "status": transaction_status,
                    "provider_fingerprint": transaction.provider_fingerprint,
                    "revision": 1,
                    **_transaction_extension_values(transaction),
                }
            )

        async def fallback() -> UpsertResult:
            ids: list[str] = []
            for index, transaction in enumerate(transactions):
                entity = await self.persist_transaction(
                    uow,
                    transaction,
                    account_id,
                    security_id=resolved_security_ids[index],
                    connection_id=connection_id,
                )
                ids.append(str(getattr(entity, "id", "")))
            return UpsertResult(inserted_ids=tuple(ids), updated_ids=())

        is_postgresql = _is_postgresql(uow.session)
        result = await bulk_upsert_transactions(
            uow.session,
            rows,
            index_elements=(
                "tenant_id",
                "provider_key",
                "connection_id",
                "external_transaction_id",
            ),
            update_columns=(
                "security_id",
                "amount",
                "currency_code",
                "amount_in_base",
                "base_currency_code",
                "fx_rate",
                "occurred_at",
                "booked_at",
                "transaction_type",
                "description",
                "quantity",
                "unit_price",
                "fee_amount",
                "fee_currency_code",
                "status",
                "provider_fingerprint",
                "provider_metadata_contract",
                "merchant_name",
                "merchant_id",
                "merchant_city",
                "merchant_country",
                "counterparty_name",
                "counterparty_account_reference",
                "merchant_category_code",
                "original_type",
                "original_status",
                "authorization_status",
                "settlement_status",
                "source_record_hash",
                "cashflow_bucket",
                "cashflow_suggestion",
                "classification_source",
                "classification_override",
                "gross_amount",
                "gross_currency_code",
                "net_amount",
                "net_currency_code",
                "tax_amount",
                "tax_currency_code",
                "refund_amount",
                "refund_currency_code",
            ),
            fallback=fallback,
        )
        if is_postgresql:
            changed = len(result.updated_ids)
            inserted = len(result.inserted_ids)
            self._last_upsert_outcome = {
                "new": inserted,
                "changed": changed,
                "unchanged": max(len(transactions) - inserted - changed, 0),
            }
        # Outbox events are emitted only on the PostgreSQL path.  The
        # per-row fallback (SQLite unit tests, mock sessions) already
        # emits ``created``/``updated`` inside :meth:`persist_transaction`,
        # so emitting again here would double-publish.
        if is_postgresql and result.total:
            await self._emit_batch_outbox(
                uow,
                transactions,
                result,
                [str(row["id"]) for row in rows],
            )
        # Count semantics match the per-row path: every input row was
        # processed (inserted, updated, or a no-op conflict-update), so
        # the processed count is the input length — not the number of
        # rows that actually changed (the WHERE-gated DO UPDATE returns
        # only changed rows, which would undercount re-syncs).
        return len(transactions)

    async def _emit_batch_outbox(
        self,
        uow: UnitOfWork,
        transactions: Sequence[CanonicalTransactionData],
        result: UpsertResult,
        generated_ids: Sequence[str],
    ) -> None:
        """Emit ``transaction.created``/``transaction.updated`` per row.

        An inserted row keeps the freshly generated uuid4 we handed it
        (``RETURNING`` returns that id), so a generated id appearing in
        ``result.inserted_ids`` identifies exactly which input row was
        created.  Updated rows keep their pre-existing id, so
        ``result.updated_ids`` are the updated entity ids in RETURNING
        (input) order; the provider key is uniform across one account's
        batch, so it is taken from the batch.

        Idempotency keys are derived from the entity id, so a re-run
        that only updates rows emits ``*.updated`` (never a second
        ``*.created``), and the outbox unique constraint keeps
        re-emission safe.  Rows whose conflict-update found no change
        are not returned by the statement and are not emitted at all.
        """
        inserted_set = set(result.inserted_ids)
        for transaction, generated_id in zip(
            transactions, generated_ids, strict=False
        ):
            if generated_id not in inserted_set:
                continue
            await outbox_entity_created(
                uow,
                tenant_id=self._tenant_id,
                entity_type="transaction",
                entity_id=generated_id,
                entity_data={
                    "provider_key": transaction.provider_key,
                    "external_transaction_id": (
                        transaction.external_transaction_id
                    ),
                    "amount": str(transaction.amount),
                    "currency_code": transaction.currency_code,
                },
                provider_key=transaction.provider_key,
            )
        provider_key = transactions[0].provider_key if transactions else None
        for entity_id in result.updated_ids:
            await outbox_entity_updated(
                uow,
                tenant_id=self._tenant_id,
                entity_type="transaction",
                entity_id=entity_id,
                changed_fields={"batch_upsert": True},
                provider_key=provider_key,
            )

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
            "provider_metadata_contract",
            "merchant_name",
            "merchant_id",
            "merchant_city",
            "merchant_country",
            "counterparty_name",
            "counterparty_account_reference",
            "merchant_category_code",
            "original_type",
            "original_status",
            "authorization_status",
            "settlement_status",
            "source_record_hash",
            "cashflow_bucket",
            "cashflow_suggestion",
            "classification_source",
            "classification_override",
            "gross_amount",
            "gross_currency_code",
            "net_amount",
            "net_currency_code",
            "tax_amount",
            "tax_currency_code",
            "refund_amount",
            "refund_currency_code",
        )
        if existing is not None:
            changed: dict[str, object] = {}
            source_correction = False
            for field in fields:
                value = getattr(transaction, field, None)
                # A user classification is authoritative.  Provider sync may
                # refresh the derived suggestion/source, but must never
                # replace an explicit category override on a later sync.
                if (
                    field == "classification_override"
                    and getattr(existing, field, None) is not None
                ):
                    continue
                if value is not None and values_differ(
                    value, getattr(existing, field, None)
                ):
                    if field == "source_record_hash":
                        source_correction = True
                    setattr(existing, field, value)
                    changed[field] = value
            if security_id is not None and values_differ(
                security_id, existing.security_id
            ):
                existing.security_id = security_id
                changed["security_id"] = security_id
            if changed:
                self._last_upsert_outcome["changed"] = (
                    self._last_upsert_outcome.get("changed", 0) + 1
                )
                existing.revision = (existing.revision or 0) + 1
                event_type = (
                    "reverse" if transaction.status == "reversed" else "update"
                )
                if transaction.refund_amount is not None:
                    event_type = "refund"
                _add_lifecycle_event(
                    uow,
                    tenant_id=self._tenant_id,
                    transaction=existing,
                    event_type=event_type,
                    payload={"changed_fields": sorted(changed)},
                    provenance=(
                        "source_correction"
                        if source_correction
                        else "provider_sync"
                    ),
                )
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    tenant_id=self._tenant_id,
                    entity_type="transaction",
                    entity_id=str(existing.id),
                    changed_fields=changed,
                    provider_key=transaction.provider_key,
                )
            else:
                self._last_upsert_outcome["unchanged"] = (
                    self._last_upsert_outcome.get("unchanged", 0) + 1
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
            **_transaction_extension_values(transaction),
        )
        uow.session.add(entity)
        self._last_upsert_outcome["new"] = (
            self._last_upsert_outcome.get("new", 0) + 1
        )
        _add_lifecycle_event(
            uow,
            tenant_id=self._tenant_id,
            transaction=entity,
            event_type="create",
            payload={
                "external_transaction_id": transaction.external_transaction_id,
                "amount": str(transaction.amount),
                "currency_code": transaction.currency_code,
            },
        )
        await uow.session.flush()
        _add_transaction_details(uow, entity, transaction)
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

    async def persist_holdings_batch(
        self,
        uow: UnitOfWork,
        holdings: Sequence[CanonicalHoldingData],
        account_id: str,
        *,
        security_ids: Sequence[str],
    ) -> int:
        """Bulk-upsert many holding snapshots for one account in one call.

        Uses the PostgreSQL ``INSERT .. ON CONFLICT DO UPDATE`` path when
        the session is bound to PostgreSQL (one round-trip, database-level
        idempotency against the ``uq_holdings_snapshot`` constraint);
        falls back to the per-row :meth:`persist_holding` loop on other
        dialects (SQLite unit tests, mock sessions).
        """
        from finance_sync.sync.upserts import (
            UpsertResult,
            _is_postgresql,  # pyright: ignore[reportPrivateUsage]
            bulk_upsert_holdings,
        )

        if len(security_ids) != len(holdings):
            msg = "security_ids length must match holdings length"
            raise ValueError(msg)

        rows: list[dict[str, Any]] = []
        for index, holding in enumerate(holdings):
            try:
                source = HoldingSource(holding.source)
            except ValueError:
                source = HoldingSource.PROVIDER_SYNC
            from uuid import uuid4

            rows.append(
                {
                    "id": uuid4(),
                    "tenant_id": self._tenant_id,
                    "account_id": account_id,
                    "security_id": security_ids[index],
                    "observed_at": holding.observed_at,
                    "source": source.value,
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
            )

        async def fallback() -> UpsertResult:
            ids: list[str] = []
            for index, holding in enumerate(holdings):
                entity = await self.persist_holding(
                    uow,
                    holding,
                    account_id,
                    security_ids[index],
                )
                ids.append(str(getattr(entity, "id", "")))
            return UpsertResult(inserted_ids=tuple(ids), updated_ids=())

        is_postgresql = _is_postgresql(uow.session)
        result = await bulk_upsert_holdings(
            uow.session,
            rows,
            index_elements=(
                "tenant_id",
                "account_id",
                "security_id",
                "observed_at",
                "source",
            ),
            update_columns=(
                "quantity",
                "cost_basis",
                "cost_basis_currency",
                "market_value",
                "currency_code",
                "price",
                "price_currency",
            ),
            fallback=fallback,
        )
        # Outbox events are emitted only on the PostgreSQL path.  The
        # per-row fallback (SQLite unit tests, mock sessions) already
        # emits ``created``/``updated`` inside :meth:`persist_holding`,
        # so emitting again here would double-publish.
        if is_postgresql and result.total:
            await self._emit_batch_outbox(
                uow,
                holdings,
                result,
                [str(row["id"]) for row in rows],
            )
        # Count semantics match the per-row path (processed rows, not
        # changed rows — see persist_transactions_batch).
        return len(holdings)

    async def _emit_batch_outbox(
        self,
        uow: UnitOfWork,
        holdings: Sequence[CanonicalHoldingData],
        result: UpsertResult,
        generated_ids: Sequence[str],
    ) -> None:
        """Emit ``holding.created``/``holding.updated`` per snapshot.

        Same id-membership contract as the transaction batch outbox: a
        generated id in ``result.inserted_ids`` identifies the created
        input row; ``result.updated_ids`` are the updated entity ids in
        RETURNING order.
        """
        inserted_set = set(result.inserted_ids)
        for holding, generated_id in zip(holdings, generated_ids, strict=False):
            if generated_id not in inserted_set:
                continue
            await outbox_entity_created(
                uow,
                tenant_id=self._tenant_id,
                entity_type="holding",
                entity_id=generated_id,
                entity_data={"observed_at": holding.observed_at.isoformat()},
                provider_key=holding.provider_key,
            )
        provider_key = holdings[0].provider_key if holdings else None
        for entity_id in result.updated_ids:
            await outbox_entity_updated(
                uow,
                tenant_id=self._tenant_id,
                entity_type="holding",
                entity_id=entity_id,
                changed_fields={"snapshot_updated": True},
                provider_key=provider_key,
            )

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
        writer: Any,
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
        self._security_persistence = SecurityPersistence(
            context.tenant_id if context is not None else ""
        )
        self._cards_persistence = CardsPersistence(
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

    async def persist_cash_balances(
        self, uow: UnitOfWork, account_id: str, account: CanonicalAccountData
    ) -> None:
        if self.context is not None:
            await self._account_persistence.persist_cash_balances(
                uow, account_id, account
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

    async def persist_transactions_batch(
        self,
        uow: UnitOfWork,
        transactions: Sequence[CanonicalTransactionData],
        account_id: str,
        *,
        security_ids: Sequence[str | None] | None = None,
        connection_id: str | None = None,
    ) -> int:
        """Bulk-upsert a list of transactions for one account.

        Requires a context (concrete persistence); the writer-only mode
        has no batch surface and falls back to per-row forwards.
        """
        if self.context is None:
            total = 0
            resolved: list[str | None] = (
                list(security_ids)
                if security_ids is not None
                else [None] * len(transactions)
            )
            for index, transaction in enumerate(transactions):
                await self._writer.persist_transaction(
                    uow,
                    transaction,
                    account_id,
                    security_id=(
                        resolved[index] if index < len(resolved) else None
                    ),
                    connection_id=connection_id,
                )
                total += 1
            return total
        return await self._transaction_persistence.persist_transactions_batch(
            uow,
            transactions,
            account_id,
            security_ids=security_ids,
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

    async def persist_holdings_batch(
        self,
        uow: UnitOfWork,
        holdings: Sequence[CanonicalHoldingData],
        account_id: str,
        *,
        security_ids: Sequence[str],
    ) -> int:
        """Bulk-upsert a list of holding snapshots for one account."""
        if self.context is None:
            total = 0
            for index, holding in enumerate(holdings):
                await self._writer.persist_holding(
                    uow,
                    holding,
                    account_id,
                    security_ids[index] if index < len(security_ids) else "",
                )
                total += 1
            return total
        return await self._holding_persistence.persist_holdings_batch(
            uow,
            holdings,
            account_id,
            security_ids=security_ids,
        )

    async def resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[object | None, str | None]:
        if self.context is not None:
            return await self._security_persistence.resolve_security_reference(
                uow, provider_key, reference
            )
        return await self._writer.resolve_security_reference(
            uow, provider_key, reference
        )

    async def persist_scheduled_payment(
        self,
        uow: UnitOfWork,
        schedule: CanonicalScheduledPaymentData,
        account_id: str,
        *,
        connection_id: str | None = None,
    ) -> object:
        if self.context is None:
            message = "scheduled-payment persistence requires context"
            raise ValueError(message)
        return await self._cards_persistence.persist_scheduled_payment(
            uow, schedule, account_id, connection_id=connection_id
        )

    async def persist_card_transaction(
        self,
        uow: UnitOfWork,
        card_transaction: CanonicalCardTransactionData,
        *,
        connection_id: str | None = None,
    ) -> object:
        if self.context is None:
            message = "card-transaction persistence requires context"
            raise ValueError(message)
        return await self._cards_persistence.persist_card_transaction(
            uow, card_transaction, connection_id=connection_id
        )


class SecurityPersistence:
    """Resolve provider security references without owning transactions."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[Security | None, str | None]:
        """Resolve ISIN-first, avoiding ambiguous ticker matches.

        Unknown but sufficiently identified provider instruments become new
        canonical securities. Ambiguous or incomplete references enter the
        existing manual-resolution queue. A previously manual-resolved queue
        item is honoured before automatic matching.
        """
        external_id = reference.provider_identifier()
        if external_id:
            queued = await uow.unresolved_securities.list(
                UnresolvedSecurity.tenant_id == self._tenant_id,
                UnresolvedSecurity.provider_key == provider_key,
                UnresolvedSecurity.external_security_id == external_id,
                limit=1,
            )
            if queued and queued[0].resolved_security_id:
                resolved = await uow.securities.get(
                    queued[0].resolved_security_id
                )
                if resolved is not None:
                    return resolved, None

        candidates: list[Security] = []
        if reference.isin:
            candidates = await uow.securities.list(
                Security.isin == reference.isin.upper()
            )
        if not candidates and reference.figi:
            candidates = await uow.securities.list(
                Security.figi == reference.figi.upper()
            )
        if (
            not candidates
            and reference.ticker
            and reference.external_id is None
        ):
            candidates = await uow.securities.list(
                Security.ticker == reference.ticker.upper()
            )
            if reference.currency_code:
                currency_matches = [
                    item
                    for item in candidates
                    if item.currency_code == reference.currency_code.upper()
                ]
                candidates = currency_matches

        if len(candidates) == 1:
            if reference.external_id:
                await self._queue_unresolved_security(
                    uow,
                    provider_key,
                    reference,
                    resolved_security_id=str(candidates[0].id),
                    resolution_method=(
                        "auto_isin"
                        if reference.isin
                        else "auto_figi"
                        if reference.figi
                        else "auto_ticker"
                    ),
                )
            return candidates[0], None
        if len(candidates) > 1:
            return None, await self._queue_unresolved_security(
                uow, provider_key, reference
            )

        can_create = bool(
            reference.isin
            or reference.figi
            or (
                reference.external_id
                and reference.ticker
                and reference.name
                and reference.currency_code
            )
        )
        if can_create:
            try:
                security_type = SecurityType(
                    reference.security_type or SecurityType.OTHER.value
                )
            except ValueError:
                security_type = SecurityType.OTHER
            from uuid import uuid4

            security = Security(
                id=uuid4(),
                isin=reference.isin.upper() if reference.isin else None,
                figi=(
                    reference.figi.upper()
                    if reference.figi and len(reference.figi) <= 12
                    else None
                ),
                ticker=(reference.ticker.upper() if reference.ticker else None),
                name=reference.name
                or reference.ticker
                or reference.isin
                or reference.figi
                or "Unknown security",
                security_type=security_type,
                currency_code=(reference.currency_code or "EUR").upper(),
            )
            uow.session.add(security)
            await uow.session.flush()
            if reference.external_id:
                await self._queue_unresolved_security(
                    uow,
                    provider_key,
                    reference,
                    resolved_security_id=str(security.id),
                    resolution_method=(
                        "auto_isin"
                        if reference.isin
                        else "auto_figi"
                        if reference.figi
                        else "provider_instrument"
                    ),
                )
            return security, None

        return None, await self._queue_unresolved_security(
            uow, provider_key, reference
        )

    async def _queue_unresolved_security(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
        *,
        resolved_security_id: str | None = None,
        resolution_method: str | None = None,
    ) -> str | None:
        """Create or refresh a provider identity mapping/queue item."""
        external_id = reference.provider_identifier()
        if not external_id:
            # No stable key means silently storing the row would itself create
            # an unresolvable duplicate stream. It is still counted by type.
            return "missing-provider-identifier"
        rows = await uow.unresolved_securities.list(
            UnresolvedSecurity.tenant_id == self._tenant_id,
            UnresolvedSecurity.provider_key == provider_key,
            UnresolvedSecurity.external_security_id == external_id,
            limit=1,
        )
        metadata = dict(reference.provider_metadata or {})
        if reference.venue:
            metadata["venue"] = reference.venue
        raw_metadata = (
            json.dumps(metadata, sort_keys=True) if metadata else None
        )
        if rows:
            unresolved = rows[0]
            unresolved.raw_isin = reference.isin
            unresolved.raw_figi = reference.figi
            unresolved.raw_ticker = reference.ticker
            unresolved.raw_name = reference.name
            unresolved.raw_currency_code = reference.currency_code
            unresolved.raw_metadata = raw_metadata
            unresolved.resolved_security_id = resolved_security_id
            unresolved.resolution_method = resolution_method
            await uow.session.flush()
        else:
            from uuid import uuid4

            uow.session.add(
                UnresolvedSecurity(
                    id=uuid4(),
                    tenant_id=self._tenant_id,
                    provider_key=provider_key,
                    external_security_id=external_id,
                    raw_isin=reference.isin,
                    raw_figi=reference.figi,
                    raw_ticker=reference.ticker,
                    raw_name=reference.name,
                    raw_currency_code=reference.currency_code,
                    raw_metadata=raw_metadata,
                    resolved_security_id=resolved_security_id,
                    resolution_method=resolution_method,
                )
            )
            await uow.session.flush()
        return external_id


class CardsPersistence:
    """Persistence boundary for bunq schedule and card records."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def persist_scheduled_payment(
        self,
        uow: UnitOfWork,
        csp: CanonicalScheduledPaymentData,
        account_id: str,
        *,
        connection_id: str | None = None,
    ) -> ScheduledPayment:
        """Create or update a ScheduledPayment from connector data.

        Idempotent: looked up by the ``(tenant, provider, external
        schedule id)`` unique constraint — a re-run updates mutable
        fields instead of inserting a duplicate.  The lookup is scoped
        to *connection_id* when provided; the scope is persisted on the
        row itself.
        """
        existing = await uow.scheduled_payments.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=csp.provider_key,
            external_schedule_id=csp.external_schedule_id,
            connection_id=connection_id,
        )

        if existing is not None:
            changed: dict[str, Any] = {}
            for field in (
                "amount",
                "currency_code",
                "frequency",
                "interval",
                "next_execution_date",
                "end_date",
                "max_executions",
                "execution_count",
                "counterparty_name",
                "counterparty_iban",
                "description",
                "status",
                "provider_metadata",
                "provider_metadata_contract",
                "merchant_id",
                "merchant_category_code",
                "original_status",
                "authorization_status",
                "settlement_status",
                "source_record_hash",
                "refund_amount",
                "refund_currency_code",
            ):
                new_val = getattr(csp, field, None)
                old_val = getattr(existing, field, None)
                if new_val is not None and values_differ(new_val, old_val):
                    setattr(existing, field, new_val)
                    changed[field] = new_val

            if changed:
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    tenant_id=self._tenant_id,
                    entity_type="scheduled_payment",
                    entity_id=str(existing.id),
                    changed_fields=changed,
                    provider_key=csp.provider_key,
                )
            return existing

        # Create new scheduled payment
        from uuid import uuid4

        frequency = (
            ScheduleFrequency(csp.frequency)
            if csp.frequency in ScheduleFrequency.__members__.values()
            else ScheduleFrequency.CUSTOM
        )
        status = (
            ScheduleStatus(csp.status)
            if csp.status in ScheduleStatus.__members__.values()
            else ScheduleStatus.ACTIVE
        )

        schedule = ScheduledPayment(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=csp.provider_key,
            connection_id=connection_id,
            external_schedule_id=csp.external_schedule_id,
            account_id=account_id,
            amount=Decimal(str(csp.amount)),
            currency_code=csp.currency_code,
            frequency=frequency,
            interval=csp.interval,
            next_execution_date=csp.next_execution_date,
            end_date=csp.end_date,
            max_executions=csp.max_executions,
            execution_count=csp.execution_count or 0,
            counterparty_name=csp.counterparty_name,
            counterparty_iban=csp.counterparty_iban,
            description=csp.description,
            status=status,
        )
        uow.session.add(schedule)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            tenant_id=self._tenant_id,
            entity_type="scheduled_payment",
            entity_id=str(schedule.id),
            entity_data={
                "provider_key": csp.provider_key,
                "external_schedule_id": csp.external_schedule_id,
                "account_id": account_id,
            },
            provider_key=csp.provider_key,
        )
        return schedule

    async def persist_card_transaction(
        self,
        uow: UnitOfWork,
        cct: CanonicalCardTransactionData,
        *,
        connection_id: str | None = None,
    ) -> CardTransaction:
        """Create or update a CardTransaction from connector data.

        Idempotent: looked up by the ``(tenant, provider, external card
        transaction id)`` unique constraint — scoped to *connection_id*
        when provided so identical card-transaction ids from two
        connections never collide.

        The canonical record's ``external_account_id`` is the *card*
        identifier for bunq (card payments are card-scoped, not
        account-scoped), so the account link is best-effort: it is set
        when the id resolves to a known account, otherwise ``None``.
        """
        existing = await uow.card_transactions.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=cct.provider_key,
            external_card_transaction_id=cct.external_card_transaction_id,
            connection_id=connection_id,
        )

        if existing is not None:
            changed: dict[str, Any] = {}
            for field in (
                "amount",
                "currency_code",
                "merchant_name",
                "merchant_city",
                "merchant_country",
                "mcc",
                "card_id",
                "card_type",
                "card_last_four",
                "occurred_at",
                "booked_at",
                "authorization_type",
                "description",
                "status",
                "provider_metadata",
                "provider_metadata_contract",
                "merchant_id",
                "merchant_category_code",
                "original_status",
                "authorization_status",
                "settlement_status",
                "source_record_hash",
                "refund_amount",
                "refund_currency_code",
            ):
                new_val = getattr(cct, field, None)
                if new_val is not None and hasattr(new_val, "model_dump"):
                    new_val = new_val.model_dump(mode="json")
                old_val = getattr(existing, field, None)
                if new_val is not None and values_differ(new_val, old_val):
                    setattr(existing, field, new_val)
                    changed[field] = new_val

            if changed:
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    tenant_id=self._tenant_id,
                    entity_type="card_transaction",
                    entity_id=str(existing.id),
                    changed_fields=changed,
                    provider_key=cct.provider_key,
                )
            return existing

        # Best-effort account resolution (card id may not be an account)
        account_id: str | None = None
        if cct.external_account_id:
            acct = await uow.accounts.get_by_external_id(
                tenant_id=self._tenant_id,
                provider_key=cct.provider_key,
                external_account_id=cct.external_account_id,
                connection_id=connection_id,
            )
            if acct is not None:
                account_id = acct.id

        # Create new card transaction
        from uuid import uuid4

        # Canonical card data carries no transaction_type — card payments
        # are always classified as card_payment unless a provider says
        # otherwise.
        raw_txn_type = getattr(cct, "transaction_type", None)
        txn_type = (
            TransactionType(raw_txn_type)
            if raw_txn_type in TransactionType.__members__.values()
            else TransactionType.CARD_PAYMENT
        )
        auth_type = (
            CardAuthorizationType(cct.authorization_type)
            if cct.authorization_type
            in CardAuthorizationType.__members__.values()
            else CardAuthorizationType.OTHER
        )
        txn_status = (
            TransactionStatus(cct.status)
            if cct.status in TransactionStatus.__members__.values()
            else TransactionStatus.PENDING
        )

        card_txn = CardTransaction(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=cct.provider_key,
            connection_id=connection_id,
            external_card_transaction_id=cct.external_card_transaction_id,
            account_id=account_id,
            amount=Decimal(str(cct.amount)),
            currency_code=cct.currency_code,
            merchant_name=cct.merchant_name,
            merchant_city=cct.merchant_city,
            merchant_country=cct.merchant_country,
            mcc=cct.mcc,
            card_id=cct.card_id,
            card_type=cct.card_type,
            card_last_four=cct.card_last_four,
            occurred_at=cct.occurred_at,
            booked_at=cct.booked_at,
            transaction_type=txn_type,
            authorization_type=auth_type,
            description=cct.description,
            status=txn_status,
            provider_metadata=cct.provider_metadata,
            provider_metadata_contract=(
                cct.provider_metadata_contract.model_dump(mode="json")
                if cct.provider_metadata_contract is not None
                else None
            ),
            merchant_id=cct.merchant_id,
            merchant_category_code=cct.merchant_category_code,
            original_status=cct.original_status,
            authorization_status=cct.authorization_status,
            settlement_status=cct.settlement_status,
            source_record_hash=cct.source_record_hash,
            refund_amount=cct.refund_amount,
            refund_currency_code=cct.refund_currency_code,
        )
        uow.session.add(card_txn)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            tenant_id=self._tenant_id,
            entity_type="card_transaction",
            entity_id=str(card_txn.id),
            entity_data={
                "provider_key": cct.provider_key,
                "external_card_transaction_id": (
                    cct.external_card_transaction_id
                ),
            },
            provider_key=cct.provider_key,
        )
        return card_txn
