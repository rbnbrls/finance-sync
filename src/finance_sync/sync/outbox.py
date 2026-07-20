"""Transactional outbox helpers.

Provides a low-level function to atomically create ``OutboxMessage``
entries inside a UnitOfWork transaction, and a convenience function
to create messages for common domain events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from finance_sync.models import OutboxMessage

if TYPE_CHECKING:
    from finance_sync.db.uow import UnitOfWork


async def add_outbox_message(
    uow: UnitOfWork,
    *,
    aggregate_id: str,
    aggregate_type: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> OutboxMessage:
    """Create and persist an ``OutboxMessage`` inside the current UoW.

    The message is added to the session but **not flushed** — the
    caller's transaction will commit (or roll back) everything
    atomically.

    Returns the created ``OutboxMessage`` instance (pre-flush, with
    a generated PK).
    """
    message = OutboxMessage(
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        event_type=event_type,
        payload=payload or {},
        idempotency_key=idempotency_key,
    )
    uow.session.add(message)
    return message


async def outbox_entity_created(
    uow: UnitOfWork,
    *,
    entity_type: str,
    entity_id: str,
    entity_data: dict[str, Any] | None = None,
    provider_key: str | None = None,
) -> OutboxMessage:
    """Convenience: emit an outbox message for ``{entity_type}.created``.

    The idempotency key is derived from ``{entity_type}:{entity_id}:created``
    so the same creation event can be safely re-published.
    """
    return await add_outbox_message(
        uow,
        aggregate_id=entity_id,
        aggregate_type=entity_type,
        event_type=f"{entity_type}.created",
        payload={
            "entity_id": entity_id,
            "entity_type": entity_type,
            "data": entity_data or {},
            "provider_key": provider_key,
        },
        idempotency_key=f"{entity_type}:{entity_id}:created",
    )


async def outbox_entity_updated(
    uow: UnitOfWork,
    *,
    entity_type: str,
    entity_id: str,
    changed_fields: dict[str, Any] | None = None,
    provider_key: str | None = None,
) -> OutboxMessage:
    """Convenience: emit an outbox message for ``{entity_type}.updated``."""
    return await add_outbox_message(
        uow,
        aggregate_id=entity_id,
        aggregate_type=entity_type,
        event_type=f"{entity_type}.updated",
        payload={
            "entity_id": entity_id,
            "entity_type": entity_type,
            "changed_fields": changed_fields or {},
            "provider_key": provider_key,
        },
        idempotency_key=f"{entity_type}:{entity_id}:updated",
    )
