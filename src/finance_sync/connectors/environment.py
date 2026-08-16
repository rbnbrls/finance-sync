"""Environment-specific connector policy."""

from __future__ import annotations

from typing import Any

from finance_sync.config.settings import Settings

STAGING_MANAGED_PROVIDERS = frozenset({"bunq", "trading212"})
STAGING_STATIC = "static"
STAGING_TEST_API = "test_api"


def is_staging_managed(provider: str, settings: Settings) -> bool:
    """Return whether staging constrains the provider's data-source choice."""
    return settings.is_staging and provider in STAGING_MANAGED_PROVIDERS


def staging_connector_config(
    provider: str,
    settings: Settings,
    *,
    data_source: str = STAGING_STATIC,
    credentials: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return an endpoint-locked static or provider test-API config."""
    supplied = credentials or {}
    if data_source not in {STAGING_STATIC, STAGING_TEST_API}:
        msg = "data_source must be 'static' or 'test_api'"
        raise ValueError(msg)

    base = settings.staging_connector_base_url.rstrip("/")
    if provider == "bunq":
        if data_source == STAGING_TEST_API:
            api_key = supplied.get("api_key", "").strip()
            if not api_key:
                msg = "A bunq sandbox API key is required"
                raise ValueError(msg)
            return (
                {"api_key": api_key},
                {
                    "base_url": "https://public-api.sandbox.bunq.com/v1",
                    "data_source": STAGING_TEST_API,
                    "full_auth": True,
                    "permitted_ips": ["*"],
                },
            )
        return (
            {"api_key": "staging-synthetic-bunq-key"},
            {
                "base_url": f"{base}/bunq/v1",
                "data_source": STAGING_STATIC,
                "full_auth": False,
            },
        )
    if provider == "trading212":
        if data_source == STAGING_TEST_API:
            api_key = supplied.get("api_key", "").strip()
            api_secret = supplied.get("api_secret", "").strip()
            if not api_key or not api_secret:
                msg = "Trading212 demo API key and secret are required"
                raise ValueError(msg)
            return (
                {"api_key": api_key, "api_secret": api_secret},
                {
                    "base_url": "https://demo.trading212.com",
                    "data_source": STAGING_TEST_API,
                    "demo": True,
                },
            )
        return (
            {"api_key": "staging-synthetic-trading212-key"},
            {
                "base_url": f"{base}/trading212",
                "data_source": STAGING_STATIC,
                "demo": True,
            },
        )
    msg = f"{provider!r} is not a staging-managed provider"
    raise ValueError(msg)
