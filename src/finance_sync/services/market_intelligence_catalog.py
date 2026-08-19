"""Read-only source catalog for the market-intelligence source layer.

Exposes the **static metadata** of every configured provider adapter —
display name, provenance/licence note, configuration link, rate-limit
policy, freshness policy and declared capabilities — as a tenant-scoped
read contract.  This is the "source metadata" half of the REST/MCP read
contracts: it tells consumers *what a provider is, under which licence
its data may be reused, how it is configured, and what it can deliver*,
independent of the runtime state that ``/market-intelligence/providers``
reports.

Safety contract (mirrors the item read service):

* never exposes provider credentials — only the *names* of the
  configuration flags/keys that turn a provider on/off;
* never exposes raw API responses or unlicensed full article text —
  the catalog only carries adapter-declared documentation strings and
  policy numbers;
* tenant-scoped: the same catalog is served to every authenticated
  tenant, but it is built from the deployment's own configuration, so a
  tenant can only ever see the providers this deployment actually runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from finance_sync.intel.provider import IntelProvider
    from finance_sync.intel.registry import IntelProviderRegistry


class IntelSourceRateLimitDTO(BaseModel):
    """Declared request rate limit of a source (non-secret)."""

    max_requests: int = Field(description="Max requests per window")
    window_seconds: int = Field(description="Window length in seconds")
    respect: bool = Field(
        description="True when the adapter enforces the limit"
    )


class IntelSourceFreshnessDTO(BaseModel):
    """Declared freshness policy of a source (non-secret)."""

    max_age_seconds: int = Field(
        description="Age after which stored data is considered stale"
    )
    min_interval_seconds: int = Field(
        description="Earliest allowed re-fetch spacing"
    )


class IntelSourceCapabilityDTO(BaseModel):
    """One capability a source offers, with its runtime availability."""

    name: str = Field(description="Capability key, e.g. 'news'")
    available: str = Field(
        description=("Runtime availability: available / degraded / unavailable")
    )


class IntelSourceDTO(BaseModel):
    """Static read projection of one market-intelligence provider."""

    provider: str = Field(description="Provider key, e.g. 'sec_press'")
    display_name: str = Field(description="Human-readable provider name")
    enabled: bool = Field(
        description="True when the provider is registered in this deployment"
    )
    license_note: str = Field(
        description=(
            "Provenance + licence terms of the source data (never a "
            "secret, never raw content)"
        )
    )
    config_url: str | None = Field(
        default=None,
        description="Link to the provider's configuration/authentication docs",
    )
    capabilities: list[IntelSourceCapabilityDTO] = Field(
        default_factory=list[IntelSourceCapabilityDTO],
        description="Capabilities offered with their runtime availability",
    )
    rate_limit: IntelSourceRateLimitDTO = Field(
        description="Declared request rate limit"
    )
    freshness: IntelSourceFreshnessDTO = Field(
        description="Declared freshness policy"
    )
    config_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Names of the configuration switches/keys that enable or "
            "disable this source (never their values)"
        ),
    )


class IntelSourceCatalogResponse(BaseModel):
    """The full source catalog served to an authenticated tenant."""

    sources: list[IntelSourceDTO]


class IntelSourceCatalogService:
    """Builds the static source catalog from the configured registry.

    The catalog is *derived* from the adapters themselves (their
    ``display_name`` / ``license_note`` / ``config_url`` class
    attributes and their rate-limit / freshness policies), so it can
    never drift from what actually runs.  Runtime availability is
    probed per capability via :meth:`IntelProvider.status`; probing is
    best-effort and never fails the whole catalog.
    """

    def __init__(self, registry: IntelProviderRegistry) -> None:
        self._registry = registry

    async def catalog(self) -> IntelSourceCatalogResponse:
        """Return the catalog of every registered provider."""
        providers: Sequence[IntelProvider] = self._registry.all()
        sources: list[IntelSourceDTO] = [
            await self._project(provider) for provider in providers
        ]
        sources.sort(key=lambda s: s.provider)
        return IntelSourceCatalogResponse(sources=sources)

    async def _project(self, provider: IntelProvider) -> IntelSourceDTO:
        """Project one provider adapter to its read DTO (best-effort)."""
        capabilities: list[IntelSourceCapabilityDTO] = []
        try:
            status = await provider.status()
            for capability in status.capabilities:
                availability = status.availability.get(capability)
                capabilities.append(
                    IntelSourceCapabilityDTO(
                        name=capability.value,
                        available=(
                            availability.value
                            if availability is not None
                            else "unavailable"
                        ),
                    )
                )
        except Exception:  # pragma: no cover — probing never fails the catalog
            capabilities = []

        # Access the limiter policy directly: the adapter's rate-limit
        # declaration is exactly what the read contract must mirror.
        rate_limit_policy = provider._rate_limiter.policy  # noqa: SLF001  # type: ignore[reportPrivateUsage]
        return IntelSourceDTO(
            provider=provider.provider_key,
            display_name=provider.display_name,
            enabled=provider.enabled,
            license_note=provider.license_note,
            config_url=provider.config_url or None,
            capabilities=capabilities,
            rate_limit=IntelSourceRateLimitDTO(
                max_requests=rate_limit_policy.max_requests,
                window_seconds=rate_limit_policy.window_seconds,
                respect=rate_limit_policy.respect,
            ),
            freshness=IntelSourceFreshnessDTO(
                max_age_seconds=int(provider.freshness.max_age.total_seconds()),
                min_interval_seconds=int(
                    provider.freshness.min_interval.total_seconds()
                ),
            ),
            config_flags=_config_flags_for(provider.provider_key),
        )


#: Mapping provider key → configuration switch/key names (names only —
#: never values).  Kept in one place so the read contract, the docs and
#: the ops runbook cannot drift apart.
PROVIDER_CONFIG_FLAGS: dict[str, list[str]] = {
    "sec": ["INTEL_SEC_ENABLED"],
    "sec_press": ["INTEL_SEC_PRESS_ENABLED"],
    "openbb": ["OPENBB_API_KEY", "OPENBB_BASE_URL", "OPENBB_RATE_LIMIT_RPS"],
}


def _config_flags_for(provider_key: str) -> list[str]:
    """Return the configuration key *names* for a provider (never values)."""
    return list(PROVIDER_CONFIG_FLAGS.get(provider_key, []))
