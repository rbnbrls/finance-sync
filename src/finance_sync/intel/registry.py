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
        )
    )
    if getattr(settings, "intel_sec_enabled", True):
        registry.register(SecEdgarProvider())
    if getattr(settings, "intel_sec_press_enabled", True):
        registry.register(SecPressReleaseProvider())
    return registry
