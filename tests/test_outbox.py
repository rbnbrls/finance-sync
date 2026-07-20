"""Tests for the transactional outbox pattern.

Covers:
- Outbox message creation helpers (add_outbox_message, outbox_entity_created)
- OutboxPublisher polling and dispatch
- Idempotency / deduplication
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

from finance_sync.models.enums import OutboxMessageStatus

# ── Test-specific metadata & base ─────────────────────────────────

_test_metadata = MetaData()
TestBase = declarative_base(metadata=_test_metadata)


class TestOutboxMessage(TestBase):
    """Outbox message model adapted for SQLite (UUID stored as BLOB)."""

    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    aggregate_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=OutboxMessageStatus.PENDING, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC), nullable=False
    )


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def engine():
    return create_async_engine("sqlite+aiosqlite://", echo=False)


@pytest.fixture
async def tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(TestBase.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(TestBase.metadata.drop_all)


@pytest.fixture
async def session_factory(engine, tables):
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


# ── Tests for outbox helpers ──────────────────────────────────────


class TestOutboxHelpers:
    """Test the outbox helper functions via direct SQLAlchemy."""

    async def test_add_outbox_message(self, session) -> None:
        """Creating a message adds it and sets correct fields."""
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.sync.outbox import add_outbox_message

        async with UnitOfWork(session) as uow:
            msg = await add_outbox_message(
                uow,
                aggregate_id="agg_1",
                aggregate_type="account",
                event_type="account.created",
                payload={"name": "Test"},
            )
            assert msg.event_type == "account.created"
            assert msg.payload == {"name": "Test"}
            assert msg.idempotency_key is None

    async def test_outbox_entity_created(self, session) -> None:
        """Gets correct event type and idempotency key."""
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.sync.outbox import outbox_entity_created

        async with UnitOfWork(session) as uow:
            msg = await outbox_entity_created(
                uow,
                entity_type="account",
                entity_id="ent_123",
                entity_data={"name": "My Account"},
                provider_key="test_provider",
            )
            assert msg.event_type == "account.created"
            assert msg.aggregate_id == "ent_123"
            assert msg.aggregate_type == "account"
            assert msg.idempotency_key == "account:ent_123:created"
            assert msg.payload["provider_key"] == "test_provider"

    async def test_outbox_entity_updated(self, session) -> None:
        """Gets correct event type for updates."""
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.sync.outbox import outbox_entity_updated

        async with UnitOfWork(session) as uow:
            msg = await outbox_entity_updated(
                uow,
                entity_type="transaction",
                entity_id="txn_456",
                changed_fields={"amount": "50.00"},
                provider_key="test",
            )
            assert msg.event_type == "transaction.updated"
            assert msg.idempotency_key == "transaction:txn_456:updated"
            assert msg.payload["changed_fields"] == {"amount": "50.00"}


# ── Tests for OutboxPublisher ─────────────────────────────────────


def _make_pending_message(event_type: str, idempotency_key: str | None = None):
    """Create a pending TestOutboxMessage with defaults."""
    return TestOutboxMessage(
        aggregate_id="a1",
        aggregate_type="test",
        event_type=event_type,
        payload="{}",
        status=OutboxMessageStatus.PENDING,
        idempotency_key=idempotency_key,
    )


class TestOutboxPublisherFetch:
    """Test fetching pending messages."""

    async def test_fetch_pending_returns_unprocessed(
        self, session_factory
    ) -> None:
        """Publisher fetches only pending messages."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        publisher = OutboxPublisher(session_factory)

        # Insert a pending message
        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()

        # Insert a sent message (should NOT be fetched)
        sent_msg = TestOutboxMessage(
            aggregate_id="a2",
            aggregate_type="test",
            event_type="test.event2",
            payload="{}",
            status=OutboxMessageStatus.SENT,
        )
        async with session_factory() as s:
            s.add(sent_msg)
            await s.commit()

        messages = await publisher._fetch_pending()
        assert len(messages) == 1
        assert messages[0].aggregate_id == "a1"

    async def test_fetch_pending_empty(self, session_factory) -> None:
        """Returns empty list when no pending messages exist."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        publisher = OutboxPublisher(session_factory)

        messages = await publisher._fetch_pending()
        assert messages == []


class TestOutboxPublisherDispatch:
    """Test dispatching to registered handlers."""

    async def test_dispatch_calls_handler(self, session_factory) -> None:
        """Handler is called with session and message."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        called = []

        async def my_handler(session, message):
            called.append((session, message))

        publisher = OutboxPublisher(session_factory)
        publisher.register_handler("test.event", my_handler)

        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        # Reload and dispatch (dispatch uses raw SQL, not ORM, so
        # it should work with any table that has outbox_messages)
        from sqlalchemy import select

        async with session_factory() as s:
            result = await s.execute(
                select(TestOutboxMessage).where(TestOutboxMessage.id == msg_id)
            )
            reloaded = result.scalar_one()

        success = await publisher._dispatch(reloaded)
        assert success
        assert len(called) == 1

    async def test_dispatch_with_no_handlers(self, session_factory) -> None:
        """Message is marked sent even without handlers."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        publisher = OutboxPublisher(session_factory)

        msg = _make_pending_message("unregistered.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        from sqlalchemy import select

        async with session_factory() as s:
            result = await s.execute(
                select(TestOutboxMessage).where(TestOutboxMessage.id == msg_id)
            )
            reloaded = result.scalar_one()

        success = await publisher._dispatch(reloaded)
        assert success

    async def test_dispatch_handler_error(self, session_factory) -> None:
        """Message is marked failed when handler raises."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        async def failing_handler(session, message):
            err_msg = "Handler failed"
            raise RuntimeError(err_msg)

        publisher = OutboxPublisher(session_factory)
        publisher.register_handler("test.event", failing_handler)

        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        from sqlalchemy import select

        async with session_factory() as s:
            result = await s.execute(
                select(TestOutboxMessage).where(TestOutboxMessage.id == msg_id)
            )
            reloaded = result.scalar_one()

        success = await publisher._dispatch(reloaded)
        assert not success

        # Message should be failed
        async with session_factory() as s:
            result = await s.execute(
                select(TestOutboxMessage).where(TestOutboxMessage.id == msg_id)
            )
            updated = result.scalar_one()
            assert updated.status == OutboxMessageStatus.FAILED
            assert "Handler failed" in (updated.error_message or "")

    async def test_wildcard_handler(self, session_factory) -> None:
        """Wildcard '*' handler receives all event types."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        called_events = []

        async def catch_all(session, message):
            called_events.append(message.event_type)

        publisher = OutboxPublisher(session_factory)
        publisher.register_handler("*", catch_all)

        msg = _make_pending_message("foo")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        from sqlalchemy import select

        async with session_factory() as s:
            result = await s.execute(
                select(TestOutboxMessage).where(TestOutboxMessage.id == msg_id)
            )
            reloaded = result.scalar_one()

        success = await publisher._dispatch(reloaded)
        assert success
        assert "foo" in called_events


class TestOutboxIdempotency:
    """Test idempotency helpers."""

    async def test_has_been_processed_true(self, session_factory) -> None:
        """Returns True when a message with the key was already sent."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        msg = TestOutboxMessage(
            aggregate_id="a1",
            aggregate_type="test",
            event_type="test.event",
            payload="{}",
            status=OutboxMessageStatus.SENT,
            idempotency_key="key:123",
        )
        async with session_factory() as s:
            s.add(msg)
            await s.commit()

        async with session_factory() as s:
            result = await OutboxPublisher.has_been_processed(s, "key:123")
            assert result is True

    async def test_has_been_processed_false(self, session_factory) -> None:
        """Returns False for keys that don't exist."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        async with session_factory() as s:
            result = await OutboxPublisher.has_been_processed(s, "nonexistent")
            assert result is False

    async def test_is_duplicate(self, session_factory) -> None:
        """Returns True when a message with the key exists (any status)."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        msg = TestOutboxMessage(
            aggregate_id="a1",
            aggregate_type="test",
            event_type="test.event",
            payload="{}",
            status=OutboxMessageStatus.PENDING,
            idempotency_key="dup:key",
        )
        async with session_factory() as s:
            s.add(msg)
            await s.commit()

        async with session_factory() as s:
            result = await OutboxPublisher.is_duplicate(s, "dup:key")
            assert result is True

        async with session_factory() as s:
            result = await OutboxPublisher.is_duplicate(s, "other")
            assert result is False


class TestOutboxPublisherRunOnce:
    """Test the run_once tick."""

    async def test_run_once_processes_pending(self, session_factory) -> None:
        """run_once fetches and dispatches pending messages."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        processed = []

        async def handler(session, message):
            processed.append(message.event_type)

        publisher = OutboxPublisher(session_factory, batch_size=10)
        publisher.register_handler("test.event", handler)

        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()

        count = await publisher.run_once()
        assert count == 1
        assert "test.event" in processed

    async def test_run_once_empty(self, session_factory) -> None:
        """run_once returns 0 when no pending messages."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        publisher = OutboxPublisher(session_factory)
        count = await publisher.run_once()
        assert count == 0

    async def test_run_once_marks_as_sent(self, session_factory) -> None:
        """After successful dispatch, message is marked sent."""
        from finance_sync.sync.outbox_publisher import OutboxPublisher

        async def handler(session, message):
            pass  # no-op success

        publisher = OutboxPublisher(session_factory)
        publisher.register_handler("test.event", handler)

        msg = _make_pending_message("test.event")
        async with session_factory() as s:
            s.add(msg)
            await s.commit()
            msg_id = msg.id

        await publisher.run_once()

        from sqlalchemy import select

        async with session_factory() as s:
            result = await s.execute(
                select(TestOutboxMessage).where(TestOutboxMessage.id == msg_id)
            )
            updated = result.scalar_one()
            assert updated.status == OutboxMessageStatus.SENT
            assert updated.published_at is not None
