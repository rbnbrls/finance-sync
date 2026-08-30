"""Persistence helpers for canonical-to-destination object references."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from finance_sync.models import DestinationObjectReference

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def record_destination_reference(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    destination_type: str,
    transaction_id: str,
    canonical_key: str,
    destination_object_id: str,
    idempotency_key: str,
    source_revision: int | None = None,
) -> None:
    """Insert or update a destination reference without leaking tenants."""
    async with session_factory() as session:
        result = await session.execute(
            select(DestinationObjectReference).where(
                DestinationObjectReference.tenant_id == tenant_id,
                DestinationObjectReference.destination_type
                == destination_type,
                DestinationObjectReference.idempotency_key
                == idempotency_key,
            )
        )
        reference = result.scalar_one_or_none()
        if reference is None:
            reference = DestinationObjectReference(
                tenant_id=tenant_id,
                destination_type=destination_type,
                transaction_id=transaction_id,
                canonical_key=canonical_key,
                destination_object_id=destination_object_id,
                idempotency_key=idempotency_key,
                direction="write",
                source_revision=source_revision,
            )
            session.add(reference)
        else:
            reference.transaction_id = transaction_id
            reference.canonical_key = canonical_key
            reference.destination_object_id = destination_object_id
            reference.source_revision = source_revision
        await session.commit()
