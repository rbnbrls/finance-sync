"""Tests for the structured logging module."""

# pyright: basic
# structlog lacks complete type stubs

from __future__ import annotations

import logging

from finance_sync.observability.logging import (
    RequestLogMiddleware,
    configure_logging,
)


class TestConfigureLogging:
    """configure_logging() sets up structlog correctly."""

    def test_default_config_does_not_raise(self) -> None:
        """Calling with default arguments should not error."""
        configure_logging(json_output=False, log_level="INFO")

    def test_json_config_does_not_raise(self) -> None:
        """JSON output configuration should not error."""
        configure_logging(json_output=True, log_level="INFO")

    def test_log_level_is_set(self) -> None:
        """The root logger's level matches the requested level."""
        configure_logging(json_output=False, log_level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_root_logger_has_structlog_formatter(self) -> None:
        """The root logger handler uses a structlog formatter."""
        configure_logging(json_output=False, log_level="INFO")
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        handler = root.handlers[0]
        from structlog.stdlib import ProcessorFormatter

        assert isinstance(handler.formatter, ProcessorFormatter)

    def test_multiple_calls_replace_handlers(self) -> None:
        """Calling configure_logging multiple times replaces handlers."""
        configure_logging(json_output=False, log_level="INFO")
        root = logging.getLogger()
        initial_count = len(root.handlers)

        configure_logging(json_output=True, log_level="DEBUG")
        assert len(root.handlers) == initial_count  # replaced, not appended


class TestRequestLogMiddleware:
    """RequestLogMiddleware structure."""

    def test_middleware_is_creatable(self) -> None:
        """The middleware can be instantiated with a dummy app."""

        async def dummy_app(
            scope: object, receive: object, send: object
        ) -> None:
            pass

        middleware = RequestLogMiddleware(dummy_app)
        assert middleware.app is dummy_app

    def test_middleware_handles_non_http_scopes(self) -> None:
        """Non-HTTP scopes (e.g. websocket) pass through unchanged."""
        received: list[bool] = []

        async def tracking_app(
            scope: object, receive: object, send: object
        ) -> None:
            received.append(True)

        import anyio

        middleware = RequestLogMiddleware(tracking_app)

        async def run() -> None:
            await middleware({"type": "websocket"}, None, None)  # type: ignore[arg-type]

        anyio.run(run)
        assert len(received) == 1
