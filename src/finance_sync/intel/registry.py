"""Provider registry for the market-intelligence source layer.

The registry is the single place where configured providers are wired
up.  Adapters whose credentials/configuration are absent are simply not
registered — capability discovery then reports them as ``unavailable``
rather than crashing, and the REST/MCP surfaces never expose provider
credentials.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from finance_sync.intel.exceptions import IntelProviderConfigError
from finance_sync.intel.provider import IntelFreshnessPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from finance_sync.config.settings import Settings
    from finance_sync.intel.provider import IntelProvider


class IntelProviderRegistry:
    """Registry of configured :class:`IntelProvider` adapters."""

    def __init__(
        self,
        *,
        providers: Sequence[IntelProvider] | None = None,
    ) -> None:
        self._providers: dict[str, IntelProvider] = {}
        if providers:
            for provider in providers:
                self.register(provider)

    # ── Registration ───────────────────────────────────────────────

    def register(self, provider: IntelProvider) -> None:
        """Register *provider* under its ``provider_key`` (replaces any
        previous adapter with the same key)."""
        if not provider.provider_key:
            msg = "IntelProvider must declare a non-empty provider_key"
            raise IntelProviderConfigError(msg)
        self._providers[provider.provider_key] = provider

    def unregister(self, provider_key: str) -> None:
        """Remove a provider; used to disable a source at runtime."""
        self._providers.pop(provider_key, None)

    # ── Lookup ─────────────────────────────────────────────────────

    def get(self, provider_key: str) -> IntelProvider | None:
        """Return the provider adapter for *provider_key* or None."""
        return self._providers.get(provider_key)

    def require(self, provider_key: str) -> IntelProvider:
        """Return the provider adapter for *provider_key* or raise."""
        provider = self.get(provider_key)
        if provider is None:
            msg = (
                f"no market-intelligence provider registered as "
                f"{provider_key!r}"
            )
            raise IntelProviderConfigError(msg)
        return provider

    def all(self) -> Sequence[IntelProvider]:
        """Return all registered providers (sorted by key)."""
        return [self._providers[k] for k in sorted(self._providers)]

    def enabled(self) -> Sequence[IntelProvider]:
        """Return providers that are not disabled."""
        return [p for p in self.all() if p.enabled]

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, provider_key: str) -> bool:
        return provider_key in self._providers


def build_intel_registry(settings: Settings) -> IntelProviderRegistry:
    """Build the configured market-intelligence provider registry.

    Provider wiring rules:

    * **OpenBB** — always registered (matches the existing enrichment
      gateway behaviour).  It runs in degraded mode when
      ``OPENBB_API_KEY`` is absent and reports ``unavailable`` for the
      news/events capabilities it cannot serve.
    * **SEC EDGAR** — a public, legally reusable source (US regulatory
      filings are public domain).  Registered by default; can be
      disabled with ``INTEL_SEC_ENABLED=false``.
    * **SEC Press Releases** — the public-domain SEC news RSS feed
      (no API key).  Registered by default; can be disabled with
      ``INTEL_SEC_PRESS_ENABLED=false``.

    Adapters requiring a user-owned subscription or API key are only
    registered after explicit configuration (future work; none are
    shipped yet).

    Each provider's freshness policy (its own refresh cadence) can be
    overridden per provider through the ``INTEL_*_FRESHNESS_*_SECONDS``
    settings; when unset the adapter's declared default applies.
    """
    from finance_sync.intel.adapters.openbb import OpenBBIntelProvider
    from finance_sync.intel.adapters.sec import SecEdgarProvider
    from finance_sync.intel.adapters.sec_press import (
        SecPressReleaseProvider,
    )

    registry = IntelProviderRegistry()
    registry.register(
        OpenBBIntelProvider(
            api_key=(
                settings.openbb_api_key.get_secret_value()
                if settings.openbb_api_key
                else None
            ),
            base_url=settings.openbb_base_url,
            request_timeout=settings.openbb_request_timeout,
            freshness=_freshness_override(
                settings.intel_openbb_freshness_max_age_seconds,
                settings.intel_openbb_freshness_min_interval_seconds,
            ),
        )
    )
    if getattr(settings, "intel_sec_enabled", True):
        registry.register(
            SecEdgarProvider(
                freshness=_freshness_override(
                    settings.intel_sec_freshness_max_age_seconds,
                    settings.intel_sec_freshness_min_interval_seconds,
                ),
            )
        )
    if getattr(settings, "intel_sec_press_enabled", True):
        registry.register(
            SecPressReleaseProvider(
                freshness=_freshness_override(
                    settings.intel_sec_press_freshness_max_age_seconds,
                    settings.intel_sec_press_freshness_min_interval_seconds,
                ),
            )
        )
    return registry


def _freshness_override(
    max_age_seconds: int | None,
    min_interval_seconds: int | None,
) -> IntelFreshnessPolicy | None:
    """Return an override :class:`IntelFreshnessPolicy` or ``None``.

    ``None`` is returned when neither bound is set, so the adapter's
    declared default freshness applies.  When only one bound is set the
    other falls back to the adapter default (the provider constructor
    merges a partial override with its own defaults).
    """
    if max_age_seconds is None and min_interval_seconds is None:
        return None
    from datetime import timedelta

    return IntelFreshnessPolicy(
        max_age=(
            timedelta(seconds=max_age_seconds)
            if max_age_seconds is not None
            else IntelFreshnessPolicy().max_age
        ),
        min_interval=(
            timedelta(seconds=min_interval_seconds)
            if min_interval_seconds is not None
            else IntelFreshnessPolicy().min_interval
        ),
    )
