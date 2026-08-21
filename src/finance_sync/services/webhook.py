"""Webhook service — CRUD, dispatch, retry, rate limiting, and delivery logging.

This service integrates with the transactional outbox: the
``OutboxPublisher`` dispatches outbox messages to a registered handler
that calls ``WebhookService.dispatch_event()``, which fans out the event
to all active webhooks subscribed to that event type.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import traceback
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import structlog

from finance_sync.models.enums import WebhookDeliveryStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.config.settings import Settings
    from finance_sync.models.outbox import OutboxMessage
    from finance_sync.models.webhook import Webhook, WebhookDeliveryLog

logger = structlog.get_logger("finance_sync.services.webhook")


# ── Rate-limiter (in-memory sliding window per webhook id) ──────────


class _SlidingWindowCounter:
    """Simple in-memory sliding-window rate counter per key.

    Not persisted across restarts — rate limiting resets on reboot,
    which is an acceptable trade-off for a self-hosted tool.
    """

    def __init__(self) -> None:
        self._windows: dict[str, list[float]] = {}

    def is_allowed(
        self, key: str, max_per_window: int, window_s: float = 60.0
    ) -> bool:
        """Check if *key* has exceeded *max_per_window* in *window_s*
        seconds."""
        now = time.monotonic()
        cutoff = now - window_s
        timestamps = self._windows.get(key, [])
        # Prune old entries
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= max_per_window:
            self._windows[key] = timestamps
            return False
        timestamps.append(now)
        self._windows[key] = timestamps
        return True


# ── Singleton rate-limiter instance ─────────────────────────────────

_rate_limiter = _SlidingWindowCounter()


def validate_webhook_url(url: str, *, resolve: bool = False) -> None:
    """Validate a webhook destination URL.

    Policy:
    * HTTPS is allowed for any host that resolves to a public address
      (private/LAN/reserved ranges stay blocked — SSRF protection).
    * HTTP is allowed only for loopback destinations (``127.0.0.1``,
      ``::1``, ``localhost``) — required for local/self-hosted webhook
      receivers and the E2E harness.
    * Every URL must include a hostname.
    """
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        msg = "Webhook URL must include a hostname"
        raise ValueError(msg)
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        addresses = []
        if resolve:
            try:
                addresses = [
                    ipaddress.ip_address(item[4][0])
                    for item in socket.getaddrinfo(
                        hostname,
                        parsed.port or 443,
                        type=socket.SOCK_STREAM,
                    )
                ]
            except OSError as exc:
                msg = "Webhook hostname could not be resolved"
                raise ValueError(msg) from exc

    def _is_loopback_host() -> bool:
        if any(a.is_loopback for a in addresses):
            return True
        host = hostname.lower()
        return host == "localhost" or host.endswith(".localhost")

    if parsed.scheme == "https":
        for address in addresses:
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                msg = "Private webhook destinations are not allowed"
                raise ValueError(msg)
    else:
        # Only loopback destinations may use plain HTTP.
        if parsed.scheme != "http":
            msg = "Webhook URL must use HTTPS or a loopback HTTP URL"
            raise ValueError(msg)
        if not _is_loopback_host():
            msg = "HTTP is only allowed for loopback webhook destinations"
            raise ValueError(msg)


# ── Service ─────────────────────────────────────────────────────────


class WebhookService:
    """Manages webhook registration, dispatch, and delivery logging."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        redis_client: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._redis = redis_client
        self._http_client: httpx.AsyncClient | None = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._settings.webhook_request_timeout_s,
                limits=httpx.Limits(
                    max_keepalive_connections=20, max_connections=50
                ),
            )
        return self._http_client

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _is_rate_allowed(self, webhook: Webhook) -> bool:
        """Use Redis, falling back to local development state."""
        if self._redis is not None:
            try:
                bucket = int(time.time() // 60)
                key = f"finance-sync:webhook-rate:{webhook.id}:{bucket}"
                count = int(await self._redis.incr(key))
                if count == 1:
                    await self._redis.expire(key, 61)
                return count <= webhook.rate_limit_max_per_minute
            except Exception:
                logger.warning("webhook_redis_rate_limit_unavailable")
        return _rate_limiter.is_allowed(
            str(webhook.id), webhook.rate_limit_max_per_minute
        )

    # ── Webhook CRUD ────────────────────────────────────────────────

    async def create_webhook(
        self,
        tenant_id: str,
        url: str,
        events: list[str],
        *,
        secret: str | None = None,
        description: str | None = None,
        rate_limit_max_per_minute: int = 60,
    ) -> Webhook:
        """Register a new webhook endpoint.

        If no *secret* is provided, a random one is generated.
        """
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models.webhook import Webhook

        validate_webhook_url(url)
        actual_secret = secret or self._generate_secret()

        webhook = Webhook(
            tenant_id=tenant_id,
            url=url,
            secret=actual_secret,
            events=events,
            description=description,
            rate_limit_max_per_minute=rate_limit_max_per_minute,
        )

        async with UnitOfWork(self._session_factory()) as uow:
            created = await uow.webhooks.add(webhook)
            await uow.commit()

        return created

    async def list_webhooks(
        self,
        tenant_id: str,
        *,
        event_type: str | None = None,
    ) -> list[Webhook]:
        """List webhooks for a tenant, optionally filtered by event type."""
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models.webhook import Webhook as WebhookModel

        async with UnitOfWork(self._session_factory()) as uow:
            filters = [WebhookModel.tenant_id == tenant_id]  # type: ignore[attr-defined]
            if event_type:
                # JSONB contains — check if event_type is in the events array
                from sqlalchemy import cast
                from sqlalchemy.dialects.postgresql import JSONB

                filters.append(
                    WebhookModel.events.contains(cast([event_type], JSONB))  # type: ignore[attr-defined]
                )
            webhooks = await uow.webhooks.list(*filters)
            return list(webhooks)

    async def get_webhook(
        self,
        webhook_id: str,
        tenant_id: str,
    ) -> Webhook | None:
        """Get a single webhook by ID (scoped to tenant)."""
        from finance_sync.db.uow import UnitOfWork as _UoW

        async with _UoW(self._session_factory()) as uow:  # type: ignore[arg-type]
            wh = await uow.webhooks.get(webhook_id)
            if wh is None or wh.tenant_id != tenant_id:
                return None
            return wh

    async def delete_webhook(self, webhook_id: str, tenant_id: str) -> bool:
        """Delete a webhook. Returns True if deleted, False if not found."""
        from finance_sync.db.uow import UnitOfWork as _UoW

        async with _UoW(self._session_factory()) as uow:  # type: ignore[arg-type]
            wh = await uow.webhooks.get(webhook_id)
            if wh is None or wh.tenant_id != tenant_id:
                return False
            await uow.webhooks.delete(wh)
            await uow.commit()
        return True

    # ── Event dispatch ──────────────────────────────────────────────

    async def dispatch_event(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
        tenant_id: str | None = None,
    ) -> int:
        """Dispatch an event to all active webhooks subscribed to *event_type*.

        Returns the number of webhooks targeted.

        This is called from the outbox handler for each outbox message.

        **Privacy policy:** deliveries are scoped to the owning tenant's
        webhooks (outbox messages carry no tenant column — the tenant is
        read from the event payload).
        """
        webhooks = await self._get_active_webhooks_for_event(
            event_type, tenant_id
        )
        if not webhooks:
            return 0

        log = logger.bind(event_type=event_type, targeted=len(webhooks))
        log.debug("webhook_dispatch_fanout")

        async with self._session_factory() as session:
            for wh in webhooks:
                await self._deliver(wh, event_type, data, event_id, session)
            # Persist every delivery-log row (and its status update) —
            # without this commit the audit trail and the retry/DLQ
            # mechanism (retry_due_deliveries) silently see nothing,
            # because the session closes and rolls the rows back.
            await session.commit()

        return len(webhooks)

    # ── Outbox handler (registered with OutboxPublisher) ────────────

    async def handle_outbox_message(
        self,
        _session: object,
        message: object,
    ) -> None:
        """Outbox handler — called by ``OutboxPublisher`` for matching events.

        This handler is registered for wildcard ``"*"`` so it intercepts
        all outbox messages.  It dispatches only event types that have
        at least one active subscribed webhook.

        The ``_session`` argument is ignored because this handler creates
        its own sessions for webhook dispatch (avoiding long-lived
        transactions in the publisher loop).
        """
        msg: OutboxMessage = message  # type: ignore[assignment]
        payload = msg.payload or {}
        # Outbox messages have no tenant column; the emitting service
        # embeds the owning tenant in the payload (see
        # finance_sync.sync.outbox) so deliveries stay tenant-scoped.
        tenant_id = getattr(msg, "tenant_id", None) or payload.get("tenant_id")
        count = await self.dispatch_event(
            event_type=msg.event_type,
            data=payload,
            event_id=str(msg.id),
            tenant_id=tenant_id,
        )
        if count > 0:
            logger.debug(
                "outbox_handler_dispatched",
                event_type=msg.event_type,
                message_id=str(msg.id),
                webhooks_targeted=count,
            )

    # ── Internal delivery logic ─────────────────────────────────────

    async def _get_active_webhooks_for_event(
        self,
        event_type: str,
        tenant_id: str | None = None,
    ) -> list[Webhook]:
        """Return active webhooks subscribed to *event_type*.

        Uses raw SQL / JSONB containment for cross-dialect compatibility.
        """
        from sqlalchemy import cast
        from sqlalchemy import select as sa_select
        from sqlalchemy.dialects.postgresql import JSONB

        from finance_sync.models.webhook import Webhook as WebhookModel

        async with self._session_factory() as session:
            stmt = (
                sa_select(WebhookModel)
                .where(
                    WebhookModel.is_active.is_(True),  # type: ignore[attr-defined]
                    WebhookModel.events.contains(cast([event_type], JSONB)),  # type: ignore[attr-defined]
                )
                .order_by(WebhookModel.created_at)  # type: ignore[attr-defined]
            )
            if tenant_id:
                stmt = stmt.where(
                    WebhookModel.tenant_id == tenant_id  # type: ignore[attr-defined]
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _deliver(
        self,
        webhook: Webhook,
        event_type: str,
        data: dict[str, Any],
        event_id: str | None,
        session: AsyncSession,
    ) -> None:
        """Deliver an event to a single webhook.

        Creates a delivery log entry, attempts the HTTP POST, and
        schedules retries on failure.
        """
        from finance_sync.models.webhook import WebhookDeliveryLog

        # Build the signed payload
        occurred_at = datetime.now(UTC)
        payload = {
            "event_type": event_type,
            "occurred_at": occurred_at.isoformat(),
            "data": data,
            "event_id": event_id,
        }

        # HMAC-SHA256 signature
        signature = self._sign_payload(payload, webhook.secret)
        payload["signature"] = signature

        # Create delivery log
        log_entry = WebhookDeliveryLog(
            webhook_id=str(webhook.id),
            tenant_id=str(webhook.tenant_id),
            event_type=event_type,
            event_id=event_id,
            payload=payload,
            status=WebhookDeliveryStatus.PENDING,
            attempt_number=1,
            max_attempts=self._settings.webhook_max_retries,
        )
        session.add(log_entry)
        # Flush to get the log entry ID
        await session.flush()
        await session.refresh(log_entry)

        # Attempt delivery
        await self._attempt_delivery(log_entry, webhook, session)

    async def _attempt_delivery(
        self,
        log_entry: WebhookDeliveryLog,
        webhook: Webhook,
        session: AsyncSession,
    ) -> None:
        """Execute a single delivery attempt."""
        from sqlalchemy import update

        from finance_sync.models.webhook import WebhookDeliveryLog as LogModel

        start = time.monotonic()
        success = False
        rate_limited = False
        status_code: int | None = None
        response_body: str | None = None
        error_msg: str | None = None

        # Rate-limit check
        if not await self._is_rate_allowed(webhook):
            logger.warning(
                "webhook_rate_limited",
                webhook_id=str(webhook.id),
                url=webhook.url,
                attempt=log_entry.attempt_number,
            )
            rate_limited = True
            status_code = 429
            error_msg = "Rate limited: too many requests in the last 60 seconds"
            log_entry.status = WebhookDeliveryStatus.RATE_LIMITED
        else:
            try:
                validate_webhook_url(webhook.url, resolve=True)
                response = await self.http_client.post(
                    webhook.url,
                    json=log_entry.payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "FinanceSync-Webhook/1.0",
                        "X-Signature-256": log_entry.payload.get(
                            "signature", ""
                        )
                        if log_entry.payload
                        else "",
                    },
                )
                status_code = response.status_code
                # Read a limited amount of response body
                body = response.text
                response_body = body[:2048] if body else None
                success = 200 <= status_code < 300
                if not success:
                    error_msg = f"HTTP {status_code}"
            except httpx.TimeoutException:
                error_msg = "Request timed out"
                status_code = 0
            except httpx.RequestError as exc:
                error_msg = f"Request error: {exc.__class__.__name__}: {exc}"
                status_code = 0
            except Exception as exc:
                error_msg = f"Unexpected error: {exc}"
                status_code = 0
                logger.error(
                    "webhook_delivery_unexpected_error",
                    webhook_id=str(webhook.id),
                    error=traceback.format_exc(),
                )

        duration_ms = int((time.monotonic() - start) * 1000)

        if success:
            log_entry.status = WebhookDeliveryStatus.DELIVERED
            log_entry.response_status_code = status_code
            log_entry.response_body = response_body
            log_entry.duration_ms = duration_ms
            logger.info(
                "webhook_delivered",
                webhook_id=str(webhook.id),
                url=webhook.url,
                status=status_code,
                duration_ms=duration_ms,
            )
        elif rate_limited:
            # Rate-limited attempts are recorded as RATE_LIMITED (not
            # FAILED) — the retry worker must not re-attempt them within
            # the same window.
            log_entry.response_status_code = status_code
            log_entry.duration_ms = duration_ms
            log_entry.error_message = error_msg
        else:
            log_entry.status = WebhookDeliveryStatus.FAILED
            log_entry.response_status_code = status_code
            log_entry.response_body = response_body
            log_entry.duration_ms = duration_ms
            log_entry.error_message = error_msg
            logger.warning(
                "webhook_delivery_failed",
                webhook_id=str(webhook.id),
                url=webhook.url,
                attempt=log_entry.attempt_number,
                error=error_msg,
                duration_ms=duration_ms,
            )

            # Schedule retry if attempts remain
            if log_entry.attempt_number < log_entry.max_attempts:
                delay = self._settings.webhook_retry_base_delay_s * (
                    2 ** (log_entry.attempt_number - 1)
                )
                next_retry = datetime.now(UTC) + timedelta(seconds=delay)
                log_entry.next_retry_at = next_retry

                logger.info(
                    "webhook_retry_scheduled",
                    webhook_id=str(webhook.id),
                    attempt=log_entry.attempt_number,
                    max_attempts=log_entry.max_attempts,
                    next_retry_at=next_retry.isoformat(),
                )

        # Persist changes to the log entry
        stmt = (
            update(LogModel)
            .where(LogModel.id == log_entry.id)  # type: ignore[attr-defined]
            .values(
                status=log_entry.status.value,
                attempt_number=log_entry.attempt_number,
                next_retry_at=log_entry.next_retry_at,
                response_status_code=log_entry.response_status_code,
                response_body=log_entry.response_body,
                duration_ms=log_entry.duration_ms,
                error_message=log_entry.error_message,
            )
        )
        await session.execute(stmt)

    # ── Retry worker ────────────────────────────────────────────────

    async def retry_due_deliveries(self) -> int:
        """Retry all failed deliveries whose ``next_retry_at`` has passed.

        Called periodically by the background worker.

        Returns the number of delivery logs retried.
        """
        from datetime import UTC, datetime

        from sqlalchemy import select as sa_select

        from finance_sync.models.webhook import (
            Webhook,
        )
        from finance_sync.models.webhook import (
            WebhookDeliveryLog as LogModel,
        )

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            stmt = (
                sa_select(LogModel)
                .where(
                    LogModel.status == WebhookDeliveryStatus.FAILED.value,  # type: ignore[attr-defined]
                    LogModel.next_retry_at.is_not(None),  # type: ignore[attr-defined]
                    LogModel.next_retry_at <= now,  # type: ignore[attr-defined]
                    LogModel.attempt_number < LogModel.max_attempts,  # type: ignore[attr-defined]
                )
                .limit(100)
            )
            result = await session.execute(stmt)
            pending = list(result.scalars().all())

        if not pending:
            return 0

        retried = 0
        for log_entry in pending:
            # Find the associated webhook
            async with self._session_factory() as s:
                wh = await s.get(Webhook, log_entry.webhook_id)

            if wh is None or not wh.is_active:
                # Webhook deleted or deactivated — skip
                async with self._session_factory() as s:
                    stmt = (
                        sa_select(LogModel).where(LogModel.id == log_entry.id)  # type: ignore[attr-defined]
                    )
                    r = await s.execute(stmt)
                    row = r.scalar_one_or_none()
                    if row:
                        from sqlalchemy import update

                        stmt_upd = (
                            update(LogModel)
                            .where(LogModel.id == row.id)  # type: ignore[attr-defined]
                            .values(
                                error_message=(
                                    "Webhook no longer active; retry cancelled"
                                )
                            )
                        )
                        await s.execute(stmt_upd)
                        await s.commit()
                continue

            # Increment attempt and retry
            log_entry.attempt_number += 1
            async with self._session_factory() as s:
                # Load fresh delivery log entry
                stmt = (
                    sa_select(LogModel).where(LogModel.id == log_entry.id)  # type: ignore[attr-defined]
                )
                r = await s.execute(stmt)
                fresh_log = r.scalar_one_or_none()
                if fresh_log is None:
                    continue

                fresh_log.attempt_number = log_entry.attempt_number
                await self._attempt_delivery(fresh_log, wh, s)
                await s.commit()
                retried += 1

        logger.info(
            "webhook_retry_worker_complete",
            retried=retried,
            total_pending=len(pending),
        )
        return retried

    # ── HMAC signing ────────────────────────────────────────────────

    @staticmethod
    def _sign_payload(payload: dict[str, Any], secret: str) -> str:
        """Create HMAC-SHA256 signature of the JSON payload."""
        serialised = json.dumps(payload, sort_keys=True, default=str)
        return hmac.new(
            secret.encode("utf-8"),
            serialised.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def verify_signature(
        payload: dict[str, Any], signature: str, secret: str
    ) -> bool:
        """Verify an HMAC-SHA256 signature against a payload.

        Consumers can use this to authenticate incoming webhook payloads::

            from finance_sync.services.webhook import WebhookService
            is_valid = WebhookService.verify_signature(
                received_payload, received_signature, stored_secret,
            )
        """
        expected = WebhookService._sign_payload(payload, secret)
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _generate_secret() -> str:
        """Generate a cryptographically random webhook secret."""
        return uuid4().hex + uuid4().hex

    # ── Event emission convenience helpers ──────────────────────────

    @classmethod
    async def emit_event(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
        tenant_id: str | None = None,
    ) -> int:
        """One-shot convenience: create a service instance, dispatch,
        and return.

        Useful for direct calls from other services that want to fire a
        webhook event without going through the outbox.
        """
        svc = cls(session_factory, settings)
        try:
            return await svc.dispatch_event(
                event_type=event_type,
                data=data,
                event_id=event_id,
                tenant_id=tenant_id,
            )
        finally:
            await svc.close()
