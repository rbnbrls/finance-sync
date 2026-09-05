"""Privacy and configuration tests for the GlitchTip integration."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

from pydantic import SecretStr

from finance_sync.config.settings import Settings
from finance_sync.observability.glitchtip import (
    configure_glitchtip,
    scrub_event,
)


def test_glitchtip_is_disabled_without_explicit_configuration() -> None:
    settings = Settings(_env_file=None)
    assert configure_glitchtip(settings) is False


def test_scrub_event_removes_financial_and_request_data() -> None:
    event = scrub_event(
        {
            "request": {
                "url": "https://finance-sync.test/api/v1/sync/"
                "123e4567-e89b-12d3-a456-426614174000?token=secret",
                "method": "POST",
                "data": {"amount": "12.34"},
                "headers": {"Authorization": "Bearer secret"},
            },
            "user": {"email": "person@example.test"},
            "extra": {"raw_payload": {"amount": "12.34"}},
            "tags": {"provider": "bunq"},
        },
        {},
    )

    scrubbed = cast("dict[str, Any]", event)
    assert scrubbed["request"] == {
        "url": "/api/v1/sync/:id",
        "method": "POST",
    }
    assert "user" not in scrubbed
    assert "extra" not in scrubbed
    assert scrubbed["tags"] == {"provider": "bunq"}


def test_glitchtip_settings_are_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("GLITCHTIP_ENABLED", "true")
    monkeypatch.setenv("GLITCHTIP_DSN", "https://public@example.test/1")
    monkeypatch.setenv("GLITCHTIP_TRACES_SAMPLE_RATE", "0.05")
    monkeypatch.setenv("GLITCHTIP_RELEASE", "release-test")

    settings = Settings(_env_file=None)
    assert settings.glitchtip_enabled is True
    assert isinstance(settings.glitchtip_dsn, SecretStr)
    assert settings.glitchtip_traces_sample_rate == 0.05
    assert settings.glitchtip_release == "release-test"


def test_scrub_event_preserves_protocol_identifier_fields() -> None:
    """Protocol UUIDs must survive scrubbing (issue: 32-hex runs were being
    redacted to ``[REDACTED]`` by the long-run regex, making GlitchTip reject
    the envelope with HTTP 400)."""
    event_id = "f1571095dd934b3197aede6b128c0705"
    trace_id = "398ca6ba62a14f2ca87767aa148d321e"
    span_id = "89cbdf0250009381"

    event = scrub_event(
        {
            "event_id": event_id,
            "message": "boom",
            "level": "error",
            "platform": "python",
            "contexts": {
                "trace": {
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "parent_span_id": None,
                }
            },
            "sdk": {"name": "sentry.python.fastapi", "version": "2.68.0"},
            "release": "0.7.3",
            "environment": "prod",
        },
        {},
    )

    scrubbed = cast("dict[str, Any]", event)
    assert scrubbed["event_id"] == event_id
    trace = scrubbed["contexts"]["trace"]
    assert trace["trace_id"] == trace_id
    assert trace["span_id"] == span_id
    assert scrubbed["release"] == "0.7.3"
    assert scrubbed["environment"] == "prod"


def test_capture_connector_exception_adds_safe_context_and_original_error() -> (
    None
):
    from finance_sync.observability.glitchtip import capture_connector_exception

    scope = MagicMock()
    scope_context = MagicMock()
    scope_context.__enter__.return_value = scope
    scope_context.__exit__.return_value = False
    scope.set_fingerprint = None
    error = ValueError("provider payload contained api_key=secret-value")

    with (
        patch(
            "finance_sync.observability.glitchtip.sentry_sdk.push_scope",
            return_value=scope_context,
        ),
        patch(
            "finance_sync.observability.glitchtip.sentry_sdk.capture_exception"
        ) as capture,
    ):
        capture_connector_exception(
            error,
            connector="trading212",
            operation="fetch_transactions",
            connection_id="connection-123",
            provider_account_id="broker-account-456",
            correlation_id="sync-run-789",
            fingerprint="incident-123",
        )

    scope.set_tag.assert_any_call("connector", "trading212")
    scope.set_tag.assert_any_call("operation", "fetch_transactions")
    scope.set_tag.assert_any_call("incident_fingerprint", "incident-123")
    scope.set_tag.assert_any_call("correlation_id", "sync-run-789")
    context = scope.set_context.call_args.args[1]
    assert context["connector"] == "trading212"
    assert context["operation"] == "fetch_transactions"
    assert context["connection_fingerprint"] != "connection-123"
    assert context["provider_account_fingerprint"] != "broker-account-456"
    capture.assert_called_once_with(error)
