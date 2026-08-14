"""Outbox integration tests against real PostgreSQL.

Port of ``tests/test_outbox.py`` (aiosqlite) that exercises the **real**
``OutboxMessage`` model against PostgreSQL: JSONB payloads, UUID primary
keys, the unique ``idempotency_key`` constraint, and the OutboxPublisher
poll/dispatch loop against a real database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from finance_sync.db.uow import UnitOfWork
from finance_sync.models import OutboxMessage
from finance_sync.models.enums import OutboxMessageStatus
from finance_sync.sync.outbox import (
    add_outbox_message,
    outbox_entity_created,
    outbox_entity_updated,
    outbox_reconciliation_completed,
)
from finance_sync.sync.outbox_publisher import OutboxPublisher

pytestmark = pytest.mark.integration


def _make_pending_message(
    event_type: str,
    idempotency_key: str | None = None,
    payload: dict | None = None,
) -> OutboxMessage:
    """Create a pending real OutboxMessage (JSONB payload)."""
    return OutboxMessage(
        aggregate_id="a1",
        aggregate_type="test",
        event_type=event_type,
        payload=payload if payload is not None else {},
        status=OutboxMessageStatus.PENDING,
        idempotency_key=idempotency_key,
    )


class TestOutboxHelpersPg:
    """Outbox helper functions against a real JSONB column."""

    async def test_add_outbox_message_roundtrip(self, session) -> None:
        async with UnitOfWork(session) as uow:
            msg = await add_outbox_message(
                uow,
                aggregate_id="agg_1",
                aggregate_type="account",
                event_type="account.created",
                payload={"name": "Test", "nested": {"iban": "NL00"}},
            )
            assert msg.event_type == "account.created"
            assert msg.payload == {"name": "Test", "nested": {"iban": "NL00"}}

        # Reload — JSONB payload must survive a real commit.
        async with session.begin():
            reloaded = await session.execute(
                select(OutboxMessage).where(
                    OutboxMessage.event_type == "account.created"  # type: ignore[attr-defined]
                )
            )
            row = reloaded.scalar_one()
        assert row.payload == {"name": "Test", "nested": {"iban": "NL00"}}

    async def test_outbox_entity_created(self, session) -> None:
        async with UnitOfWork(session) as uow:
            msg = await outbox_entity_created(
                uow,
                entity_type="account",
                entity_id="ent_123",
                entity_data={"name": "My Account"},
                provider_key="test_provider",
            )
            assert msg.event_type == "account.created"
            assert msg.idempotency_key == "account:ent_123:created"
            assert msg.payload["provider_key"] == "test_provider"
            assert msg.payload["data"]["name"] == "My Account"

    async def test_outbox_entity_updated(self, session) -> None:
        async with UnitOfWork(session) as uow:
            msg = await outbox_entity_updated(
                uow,
                entity_type="transaction",
                entity_id="txn_456",
                changed_fields={"amount": "50.00"},
                provider_key="test",
            )
            assert msg.event_type == "transaction.updated"
            assert msg.payload["changed_fields"] == {"amount": "50.00"}

    async def test_outbox_reconciliation_completed(self, session) -> None:
        async with UnitOfWork(session) as uow:
            msg = await outbox_reconciliation_completed(
                uow,
                run_id="run_abc123",
                tenant_id="tenant_1",
                finding_count=5,
                summary={"by_kind": {"duplicate_transaction": 3}},
            )
            assert msg.event_type == "reconciliation.completed"
            assert msg.payload["finding_count"] == 5
            assert (
                msg.payload["summary"]["by_kind"]["duplicate_transaction"] == 3
            )

    async def test_duplicate_idempotency_key_rejected(self, session) -> None:
        """The real unique constraint on idempotency_key enforces dedup."""
        async with UnitOfWork(session) as uow:
            await add_outbox_message(
                uow,
                aggregate_id="agg-dup",
                aggregate_type="test",
                event_type="test.event",
                idempotency_key="dup:key:1",
            )

        with pytest.raises(IntegrityError):
            async with UnitOfWork(session) as uow:
                await add_outbox_message(
                    uow,
                    aggregate_id="agg-dup-2",
                    aggregate_type="test",
                    event_type="test.event",
                    idempotency_key="dup:key:1",
                )


class TestOutboxPublisherPg:
    """OutboxPublisher polling/dispatch against real PostgreSQL."""

    async def test_fetch_pending_only(self, session_factory) -> None:
        publisher = OutboxPublisher(session_factory)

        pending = _make_pending_message("test.event")
        sent = _make_pending_message("test.event2")
        sent.status = OutboxMessageStatus.SENT

        async with session_factory() as s:
            s.add_all([pending, sent])
            await s.commit()

        messages = await publisher._fetch_pending()
        assert len(messages) == 1
        assert messages[0].event_type == "test.event"

    async def test_count_pending(self, session_factory) -> None:
        publisher = OutboxPublisher(session_factory)
        assert await publisher._count_pending() == 0

        for i in range(3):
            async with session_factory() as s:
                s.add(_make_pending_message(f"test.event.{i}"))
                await s.commit()

        assert await publisher._count_pending() == 3

    async def test_dispatch_calls_handler_and_marks_sent(
        self, session_factory
    ) -> None:
        called: list[str] = []

        async def my_handler(session, message) -> None:
            called.append(message.event_type)

        publisher = OutboxPublisher(session_factory)
        publisher.register_handler("test.event", my_handler)

        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        async with session_factory() as s:
            reloaded = (
                await s.execute(
                    select(OutboxMessage).where(OutboxMessage.id == msg_id)
                )
            ).scalar_one()

        success = await publisher._dispatch(reloaded)
        assert success
        assert called == ["test.event"]

        async with session_factory() as s:
            updated = (
                await s.execute(
                    select(OutboxMessage).where(OutboxMessage.id == msg_id)
                )
            ).scalar_one()
        assert updated.status == OutboxMessageStatus.SENT
        assert updated.published_at is not None

    async def test_dispatch_handler_error_marks_failed(
        self, session_factory
    ) -> None:
        async def failing_handler(session, message) -> None:
            err_msg = "Handler failed"
            raise RuntimeError(err_msg)

        publisher = OutboxPublisher(session_factory)
        publisher.register_handler("test.event", failing_handler)

        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        async with session_factory() as s:
            reloaded = (
                await s.execute(
                    select(OutboxMessage).where(OutboxMessage.id == msg_id)
                )
            ).scalar_one()

        success = await publisher._dispatch(reloaded)
        assert not success

        async with session_factory() as s:
            updated = (
                await s.execute(
                    select(OutboxMessage).where(OutboxMessage.id == msg_id)
                )
            ).scalar_one()
        assert updated.status == OutboxMessageStatus.FAILED
        assert "Handler failed" in (updated.error_message or "")

    async def test_wildcard_handler(self, session_factory) -> None:
        caught: list[str] = []

        async def catch_all(session, message) -> None:
            caught.append(message.event_type)

        publisher = OutboxPublisher(session_factory)
        publisher.register_handler("*", catch_all)

        msg = _make_pending_message("any.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        async with session_factory() as s:
            reloaded = (
                await s.execute(
                    select(OutboxMessage).where(OutboxMessage.id == msg_id)
                )
            ).scalar_one()

        assert await publisher._dispatch(reloaded)
        assert caught == ["any.event"]

    async def test_run_once_processes_pending(self, session_factory) -> None:
        processed: list[str] = []

        async def handler(session, message) -> None:
            processed.append(message.event_type)

        publisher = OutboxPublisher(session_factory, batch_size=10)
        publisher.register_handler("test.event", handler)

        async with session_factory() as s:
            s.add(_make_pending_message("test.event"))
            await s.commit()

        count = await publisher.run_once()
        assert count == 1
        assert processed == ["test.event"]

        # Message is now sent — a second tick does nothing.
        assert await publisher.run_once() == 0


class TestOutboxIdempotencyPg:
    async def test_has_been_processed(self, session_factory) -> None:
        msg = _make_pending_message("test.event", idempotency_key="key:123")
        msg.status = OutboxMessageStatus.SENT
        async with session_factory() as s:
            s.add(msg)
            await s.commit()

        async with session_factory() as s:
            assert (
                await OutboxPublisher.has_been_processed(s, "key:123") is True
            )
            assert (
                await OutboxPublisher.has_been_processed(s, "missing") is False
            )

    async def test_is_duplicate(self, session_factory) -> None:
        msg = _make_pending_message("test.event", idempotency_key="dup:key")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()

        async with session_factory() as s:
            assert await OutboxPublisher.is_duplicate(s, "dup:key") is True
            assert await OutboxPublisher.is_duplicate(s, "other") is False


class TestOutboxTimestampTz:
    """timestamptz columns store tz-aware values on PostgreSQL."""

    async def test_created_at_is_utc_aware(self, session_factory) -> None:
        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        async with session_factory() as s:
            reloaded = (
                await s.execute(
                    select(OutboxMessage).where(OutboxMessage.id == msg_id)
                )
            ).scalar_one()

        assert reloaded.created_at is not None
        assert reloaded.created_at.tzinfo is not None
        assert reloaded.created_at.utcoffset() == UTC.utcoffset(
            datetime.now(UTC)
        )
