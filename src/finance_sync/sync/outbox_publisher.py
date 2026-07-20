"""OutboxPublisher — background worker that polls and dispatches outbox
messages.

The publisher runs in a loop, polling ``OutboxMessage`` records with
``status='pending'`` and dispatching them to registered handler
functions.  Idempotency keys prevent duplicate processing.

Usage::

    publisher = OutboxPublisher(
        session_factory=container.session_factory,
        poll_interval=5.0,
        batch_size=20,
    )

    # Register a handler for specific event types
    publisher.register_handler(
        "account.created",
        my_account_created_handler,
    )

    # Start the polling loop (runs until cancelled)
    await publisher.run_once()
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from finance_sync.models import OutboxMessage
from finance_sync.models.enums import OutboxMessageStatus

if TYPE_CHECKING:
    from sqlalchemy import Result
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger("finance_sync.sync.outbox_publisher")

# Handler type: async callable receiving (session, message)
OutboxHandler = Callable[
    [object, OutboxMessage],
    Awaitable[None],
]


class OutboxPublisher:
    """Polls and dispatches pending outbox messages.

    Handlers are registered per event type.  A single message is
    dispatched to **all** handlers registered for its event type.
    After all handlers complete successfully the message is marked
    ``sent``.  If any handler fails the message is marked ``failed``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_interval: float = 5.0,
        batch_size: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._handlers: dict[str, list[OutboxHandler]] = {}
        self._running = False

    # ── Handler registration ───────────────────────────────────────

    def register_handler(
        self,
        event_type: str,
        handler: OutboxHandler,
    ) -> None:
        """Register a handler for *event_type*.

        Wildcard ``"*"`` matches **all** event types (catch-all).
        """
        self._handlers.setdefault(event_type, []).append(handler)
        logger.debug(
            "handler_registered",
            event_type=event_type,
            handler=handler.__name__,
        )

    # ── Polling loop ───────────────────────────────────────────────

    async def run_once(self) -> int:
        """Poll pending messages and dispatch them.

        Returns the number of messages processed in this tick.
        """
        messages = await self._fetch_pending()

        if not messages:
            return 0

        log = logger.bind(batch_size=len(messages))
        log.debug("outbox_poll_found_messages")

        processed = 0
        for msg in messages:
            success = await self._dispatch(msg)
            if success:
                processed += 1

        log.info("outbox_tick_complete", processed=processed)
        return processed

    async def run_forever(self) -> None:
        """Run the polling loop indefinitely.

        Call ``cancel()`` from another task to stop.
        """
        self._running = True
        log = logger.bind(poll_interval=self._poll_interval)

        while self._running:
            try:
                count = await self.run_once()
                if count == 0 and self._running:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                log.info("outbox_publisher_cancelled")
                self._running = False
                break
            except Exception:
                log.error(
                    "outbox_poll_error",
                    error=traceback.format_exc(),
                )
                if self._running:
                    await asyncio.sleep(self._poll_interval)

    def cancel(self) -> None:
        """Signal the polling loop to stop at the next iteration."""
        self._running = False

    # ── Internal ───────────────────────────────────────────────────

    async def _fetch_pending(self) -> list[OutboxMessage]:
        """Return pending messages ordered by creation time."""
        from sqlalchemy import select

        async with self._session_factory() as session:
            stmt = (
                select(OutboxMessage)
                .where(
                    OutboxMessage.status == OutboxMessageStatus.PENDING  # type: ignore[attr-defined]
                )
                .order_by(OutboxMessage.created_at)  # type: ignore[attr-defined]
                .limit(self._batch_size)
            )
            result: Result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _dispatch(
        self,
        message: OutboxMessage,
    ) -> bool:
        """Dispatch a single message to registered handlers.

        Returns ``True`` on success, ``False`` on failure.
        """
        from datetime import UTC, datetime

        from sqlalchemy import update

        handlers = self._handlers.get(
            message.event_type, []
        ) + self._handlers.get("*", [])

        if not handlers:
            # No handlers registered — mark as sent (nothing to do)
            await self._mark_sent(message)
            return True

        success = True
        errors: list[str] = []

        async with self._session_factory() as session:
            for handler in handlers:
                try:
                    await handler(session, message)
                except Exception:
                    tb = traceback.format_exc()
                    errors.append(tb)
                    logger.error(
                        "handler_failed",
                        event_type=message.event_type,
                        message_id=str(message.id),
                        handler=handler.__name__,
                        error=tb,
                    )
                    success = False

            # Update message status
            now = datetime.now(UTC)
            if success:
                stmt = (
                    update(OutboxMessage)
                    .where(OutboxMessage.id == message.id)  # type: ignore[attr-defined]
                    .values(
                        status=OutboxMessageStatus.SENT,
                        published_at=now,
                    )
                )
            else:
                stmt = (
                    update(OutboxMessage)
                    .where(OutboxMessage.id == message.id)  # type: ignore[attr-defined]
                    .values(
                        status=OutboxMessageStatus.FAILED,
                        error_message="; ".join(errors)[:2048],
                    )
                )
            await session.execute(stmt)
            await session.commit()

        return success

    async def _mark_sent(self, message: OutboxMessage) -> None:
        """Mark a message as sent without a handler dispatch."""
        from datetime import UTC, datetime

        from sqlalchemy import update

        async with self._session_factory() as session:
            stmt = (
                update(OutboxMessage)
                .where(OutboxMessage.id == message.id)  # type: ignore[attr-defined]
                .values(
                    status=OutboxMessageStatus.SENT,
                    published_at=datetime.now(UTC),
                )
            )
            await session.execute(stmt)
            await session.commit()

    # ── Idempotency helpers ────────────────────────────────────────

    @staticmethod
    async def has_been_processed(
        session: AsyncSession,
        idempotency_key: str,
    ) -> bool:
        """Check whether a message with the given key was already sent."""
        from sqlalchemy import select

        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.idempotency_key == idempotency_key,  # type: ignore[attr-defined]
                OutboxMessage.status == OutboxMessageStatus.SENT,  # type: ignore[attr-defined]
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def is_duplicate(
        session: AsyncSession,
        idempotency_key: str,
    ) -> bool:
        """Check if a message with the same key already exists (any status)."""
        from sqlalchemy import select

        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.idempotency_key == idempotency_key  # type: ignore[attr-defined]
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None
