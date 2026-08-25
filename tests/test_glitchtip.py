"""Privacy and configuration tests for the GlitchTip integration."""

from __future__ import annotations

from typing import Any, cast

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
