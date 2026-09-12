"""Canonical spending detail and explicit user override endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.db.uow import UnitOfWork
from finance_sync.dependencies import get_db
from finance_sync.models import (
    DestinationObjectReference,
    Transaction,
    TransactionAnnotation,
    TransactionLifecycleEvent,
    TransactionOverride,
    TransactionSourceReference,
    TransactionSplit,
)
from finance_sync.models.enums import TransactionType
from finance_sync.sync.persistence import TransactionPersistence

router = APIRouter(prefix="/transactions", tags=["spending"])


class SpendingOverrideRequest(BaseModel):
    field_name: str = Field(min_length=1, max_length=64)
    value: Any


class SpendingSplitRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    amount: str
    currency_code: str = Field(min_length=3, max_length=3)
    percentage: str | None = None
    category_suggestion: dict[str, Any] | None = None
    destination: str | None = None


class SpendingEventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict)


class DataQualityCorrectionRequest(BaseModel):
    """Audited correction for fields that block data-quality checks."""

    transaction_type: TransactionType | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)


@router.patch(
    "/{transaction_id}/data-quality-correction",
    response_model=dict[str, Any],
)
async def correct_data_quality_transaction(
    transaction_id: str,
    body: DataQualityCorrectionRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Apply an explicit, auditable correction to a canonical transaction.

    This is deliberately limited to classification and unit price. Source
    records remain intact and the change is recorded as an override plus a
    lifecycle event so a later sync can be reviewed safely.
    """
    if body.transaction_type is None and body.unit_price is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide transaction_type or unit_price.",
        )
    transaction = await _load_transaction(db, auth.tenant_id, transaction_id)
    actor = str(auth.user.id) if auth.user is not None else None
    source_revision = transaction.revision
    transaction.revision = (transaction.revision or 0) + 1
    changes: dict[str, Any] = {}
    if body.transaction_type is not None:
        transaction.transaction_type = body.transaction_type
        changes["transaction_type"] = body.transaction_type.value
    if body.unit_price is not None:
        transaction.unit_price = body.unit_price
        changes["unit_price"] = str(body.unit_price)
    db.add(
        TransactionLifecycleEvent(
            tenant_id=auth.tenant_id,
            transaction_id=transaction.id,
            event_type="update",
            idempotency_key=f"data-quality-correction:{transaction.id}:{transaction.revision}",
            payload={"changes": changes},
            actor=actor,
            provenance="user_override",
            source_revision=source_revision,
        )
    )
    for field_name, value in changes.items():
        db.add(
            TransactionOverride(
                tenant_id=auth.tenant_id,
                transaction_id=transaction.id,
                field_name=field_name,
                value={"value": value},
                actor=actor,
                provenance="user_override",
            )
        )
    await db.commit()
    return {"transaction_id": str(transaction.id), "changes": changes}


class SpendingDetailResponse(BaseModel):
    transaction_id: str
    provider_metadata_contract: dict[str, Any] | None = None
    source_references: list[dict[str, Any]] = Field(
        default_factory=lambda: list[dict[str, Any]]()
    )
    splits: list[dict[str, Any]] = Field(
        default_factory=lambda: list[dict[str, Any]]()
    )
    annotations: list[dict[str, Any]] = Field(
        default_factory=lambda: list[dict[str, Any]]()
    )
    overrides: list[dict[str, Any]] = Field(
        default_factory=lambda: list[dict[str, Any]]()
    )
    lifecycle_events: list[dict[str, Any]] = Field(
        default_factory=lambda: list[dict[str, Any]]()
    )
    destination_status: list[dict[str, Any]] = Field(
        default_factory=lambda: list[dict[str, Any]]()
    )


async def _load_transaction(
    db: AsyncSession, tenant_id: str, transaction_id: str
) -> Transaction:
    result = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.tenant_id == tenant_id,
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )
    return transaction


def _dump(row: Any) -> dict[str, Any]:
    return {
        key: value
        for key in (
            "id",
            "object_type",
            "external_ids",
            "provider_revisions",
            "annotation_type",
            "content_hash",
            "mime_type",
            "safe_reference",
            "owner",
            "retention_until",
            "destination_reference",
            "amount",
            "currency_code",
            "percentage",
            "category_suggestion",
            "destination",
            "idempotency_key",
            "provenance",
            "field_name",
            "value",
            "actor",
            "event_type",
            "payload",
            "source_revision",
            "destination_type",
            "canonical_key",
            "destination_object_id",
            "direction",
        )
        if (value := getattr(row, key, None)) is not None
    }


@router.get("/{transaction_id}/spending", response_model=SpendingDetailResponse)
async def spending_detail(
    transaction_id: str,
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
) -> SpendingDetailResponse:
    transaction = await _load_transaction(db, auth.tenant_id, transaction_id)
    queries = (
        TransactionSourceReference,
        TransactionSplit,
        TransactionAnnotation,
        TransactionOverride,
        TransactionLifecycleEvent,
        DestinationObjectReference,
    )
    rows: list[list[Any]] = []
    for model in queries:
        result = await db.execute(
            select(model).where(
                model.transaction_id == transaction.id,  # type: ignore[attr-defined]
                model.tenant_id == auth.tenant_id,  # type: ignore[attr-defined]
            )
        )
        rows.append([_dump(item) for item in result.scalars().all()])
    return SpendingDetailResponse(
        transaction_id=str(transaction.id),
        provider_metadata_contract=transaction.provider_metadata_contract,
        source_references=rows[0],
        splits=rows[1],
        annotations=rows[2],
        overrides=rows[3],
        lifecycle_events=rows[4],
        destination_status=rows[5],
    )


@router.post("/{transaction_id}/override", response_model=dict[str, Any])
async def create_spending_override(
    transaction_id: str,
    body: SpendingOverrideRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    transaction = await _load_transaction(db, auth.tenant_id, transaction_id)
    override = TransactionOverride(
        tenant_id=auth.tenant_id,
        transaction_id=transaction.id,
        field_name=body.field_name,
        value={"value": body.value},
        actor=str(auth.user.id) if auth.user is not None else None,
        provenance="user_override",
    )
    db.add(override)
    if body.field_name == "classification_override" and isinstance(
        body.value, str
    ):
        transaction.classification_override = body.value
    await db.commit()
    return {
        "id": str(override.id),
        "transaction_id": str(transaction.id),
        "field_name": body.field_name,
    }


@router.delete("/{transaction_id}", response_model=dict[str, Any])
async def tombstone_spending_transaction(
    transaction_id: str,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Tombstone a transaction without deleting source history."""
    await _load_transaction(db, auth.tenant_id, transaction_id)
    actor = str(auth.user.id) if auth.user is not None else None
    async with UnitOfWork(db) as uow:
        entity = await TransactionPersistence(
            auth.tenant_id
        ).tombstone_transaction(uow, transaction_id, actor=actor)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )
    return {
        "id": str(entity.id),
        "transaction_id": str(entity.id),
        "status": str(entity.status),
        "tombstoned_at": entity.tombstoned_at,
    }


@router.post("/{transaction_id}/split", response_model=dict[str, Any])
async def create_spending_split(
    transaction_id: str,
    body: SpendingSplitRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    transaction = await _load_transaction(db, auth.tenant_id, transaction_id)
    existing = await db.scalar(
        select(TransactionSplit).where(
            TransactionSplit.tenant_id == auth.tenant_id,
            TransactionSplit.transaction_id == transaction.id,
            TransactionSplit.idempotency_key == body.idempotency_key,
        )
    )
    if existing is not None:
        return {"id": str(existing.id), "transaction_id": str(transaction.id)}
    split = TransactionSplit(
        tenant_id=auth.tenant_id,
        transaction_id=transaction.id,
        idempotency_key=body.idempotency_key,
        amount=Decimal(body.amount),
        currency_code=body.currency_code.upper(),
        percentage=(
            Decimal(body.percentage) if body.percentage is not None else None
        ),
        category_suggestion=body.category_suggestion,
        destination=body.destination,
        provenance="user_override",
    )
    db.add(split)
    await db.commit()
    return {"id": str(split.id), "transaction_id": str(transaction.id)}


@router.post("/{transaction_id}/event", response_model=dict[str, Any])
async def create_spending_event(
    transaction_id: str,
    body: SpendingEventRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    transaction = await _load_transaction(db, auth.tenant_id, transaction_id)
    existing = await db.scalar(
        select(TransactionLifecycleEvent).where(
            TransactionLifecycleEvent.tenant_id == auth.tenant_id,
            TransactionLifecycleEvent.transaction_id == transaction.id,
            TransactionLifecycleEvent.idempotency_key == body.idempotency_key,
        )
    )
    if existing is not None:
        return {"id": str(existing.id), "transaction_id": str(transaction.id)}
    event = TransactionLifecycleEvent(
        tenant_id=auth.tenant_id,
        transaction_id=transaction.id,
        event_type=body.event_type,
        idempotency_key=body.idempotency_key,
        payload=body.payload,
        actor=str(auth.user.id) if auth.user is not None else None,
        provenance="user_override",
        source_revision=transaction.revision,
    )
    db.add(event)
    await db.commit()
    return {"id": str(event.id), "transaction_id": str(transaction.id)}
