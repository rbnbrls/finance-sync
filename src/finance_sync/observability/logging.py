"""Structured logging with structlog.

Provides a ``configure_logging()`` call that replaces the standard-library
root handler with structlog processors, and an ASGI middleware that logs
every HTTP request with duration and status.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import structlog


def configure_logging(
    *, json_output: bool = False, log_level: str = "INFO"
) -> None:
    """Configure structlog as the primary logging backend.

    Parameters
    ----------
    json_output:
        When *True* emit JSON lines suitable for production observability
        pipelines.  When *False* (default) use coloured console output.

    log_level:
        Minimum log level for the root logger (e.g. ``"INFO"``,
        ``"DEBUG"``).
    """
    # ── shared processors used for foreign (stdlib) log records ────
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.ExtraAdder(),
    ]

    if json_output:
        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    else:
        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.dev.ConsoleRenderer(),
            ],
        )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Replace existing handlers so we don't get duplicate output
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level.upper())

    # ── structlog configuration ────────────────────────────────────
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


class RequestLogMiddleware:
    """ASGI middleware that logs every HTTP request with duration + status.

    Each request receives a ``request_id`` (from the ``X-Request-ID``
    header or a new UUID) that is bound to structlog's context vars so
    that all log entries within the request scope carry it automatically.
    The same ID is set on the response ``X-Request-ID`` header.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract or generate a request ID
        headers_raw = dict(scope.get("headers", []))
        incoming_id = headers_raw.get(b"x-request-id", b"")
        request_id = (
            incoming_id.decode("ascii", errors="replace")
            if incoming_id
            else uuid.uuid4().hex
        )

        # Bind to structlog context so downstream calls pick it up
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        method = scope.get("method", "")
        path = scope.get("path", "")
        start = time.perf_counter()
        status_code: list[int | None] = [None]

        async def wrapped_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_code[0] = message.get("status")
                # Inject request_id into response headers
                headers = message.get("headers", [])
                headers.append((b"x-request-id", request_id.encode("ascii")))
            await send(message)

        await self.app(scope, receive, wrapped_send)

        duration_s = time.perf_counter() - start
        logger = structlog.get_logger("finance_sync.access")
        logger.info(
            "request completed",
            method=method,
            path=path,
            status=status_code[0],
            duration_ms=round(duration_s * 1000, 2),
        )
