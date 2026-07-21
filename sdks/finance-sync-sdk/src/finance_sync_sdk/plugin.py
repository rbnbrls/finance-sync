"""ConnectorPlugin and ExporterPlugin base classes for the finance-sync-sdk.

Third-party developers subclass these to create connectors or exporters
that the finance-sync host application can discover and load at runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from finance_sync_sdk.exceptions import ConnectorError
from finance_sync_sdk.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    ConnectorHealth,
    ExportRequest,
    ExportResult,
    RawAccount,
    RawTransaction,
)
from finance_sync_sdk.rate_limiter import RateLimitPolicy, RateLimiter

if TYPE_CHECKING:
    from datetime import datetime


# ═══════════════════════════════════════════════════════════════════════
# ConnectorPlugin
# ═══════════════════════════════════════════════════════════════════════


class ConnectorPlugin(ABC):
    """Abstract base for a third-party financial-data connector plugin.

    Subclasses **must** override:

    * :meth:`authenticate` — validate/refresh credentials.
    * :meth:`fetch_accounts` — return raw provider accounts.
    * :meth:`fetch_transactions` — return raw provider transactions.
    * :attr:`name` — short provider key (e.g. ``"mybank"``).

    Subclasses **may** override:

    * :meth:`transform_accounts` / :meth:`transform_transactions` — map
      raw data to canonical models.
    * :meth:`health` — lightweight connectivity check.
    * :class:`config_schema` — Pydantic model for configuration validation.

    Lifecycle::

        plugin = MyBankPlugin(config=ConnectorConfig(...))
        await plugin.authenticate()
        accounts = await plugin.fetch_accounts()
        canonical = plugin.transform_accounts(accounts)
    """

    #: Human-readable display name (defaults to ``name``).
    display_name: str = ""

    #: Semantic version of this plugin.
    plugin_version: str = "0.1.0"

    #: Pydantic model class for validating ``ConnectorConfig.options``.
    #: Set to ``None`` to accept any options.
    config_schema: type[Any] | None = None

    #: Optional rate-limit policy.  When set, the base class wraps
    #: ``fetch_accounts`` and ``fetch_transactions`` with rate-limited,
    #: auto-retrying wrappers.
    rate_limit_policy: RateLimitPolicy | None = None

    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config
        self._authenticated = False
        self._rate_limiter = (
            RateLimiter(self.rate_limit_policy)
            if self.rate_limit_policy
            else None
        )

        # Validate config options against schema if provided
        if self.config_schema is not None and config.options:
            self.config_schema(**config.options)

    # ── Required overrides ─────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Short unique connector key, e.g. ``\"mybank\"`` or ``\"csv_import\"``."""

    @abstractmethod
    async def authenticate(self) -> None:
        """Obtain or validate provider credentials.

        Raise :class:`PermanentError` on invalid secrets.
        Raise :class:`TransientError` on temporary provider unavailability.

        Implementation must use ``self.config.credentials``.
        """

    @abstractmethod
    async def fetch_accounts(self) -> list[RawAccount]:
        """Return all accounts accessible with the current credentials.

        May be called only after a successful :meth:`authenticate`.
        """

    @abstractmethod
    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Return transactions modified since *since*.

        Args:
            since:  Only return transactions occurring on or after this time.
            account_id:  If set, scope the fetch to a single provider account.
            limit:  Maximum number of transactions to return per page / call.
        """

    # ── Optional overrides ─────────────────────────────────────────────

    async def health(self) -> ConnectorHealth:
        """Lightweight connectivity check.

        Default implementation calls :meth:`authenticate` — override for a
        lighter check (e.g. a HEAD request to a status endpoint).
        """
        try:
            await self.authenticate()
            return ConnectorHealth(
                healthy=True,
                provider_type=self.name,
            )
        except ConnectorError as exc:
            return ConnectorHealth(
                healthy=False,
                message=str(exc),
                provider_type=self.name,
            )

    def transform_accounts(
        self,
        raw: list[RawAccount],
    ) -> list[CanonicalAccountData]:
        """Transform raw provider accounts to canonical form.

        The default implementation copies matching fields by name.
        Override for provider-specific normalisation.
        """
        return [
            CanonicalAccountData(
                provider_key=self.name,
                external_account_id=r.external_account_id,
                name=r.name,
                account_type=r.account_type,
                account_subtype=r.account_subtype,
                currency_code=r.currency_code,
                current_balance=r.current_balance,
                available_balance=r.available_balance,
                iso_currency_code=r.iso_currency_code,
                provider_metadata=r.provider_metadata,
            )
            for r in raw
        ]

    def transform_transactions(
        self,
        raw: list[RawTransaction],
    ) -> list[CanonicalTransactionData]:
        """Transform raw provider transactions to canonical form.

        The default implementation copies matching fields by name.
        Override for provider-specific normalisation.
        """
        return [
            CanonicalTransactionData(
                provider_key=self.name,
                external_transaction_id=r.external_transaction_id,
                external_account_id=r.external_account_id,
                amount=r.amount,
                currency_code=r.currency_code,
                occurred_at=r.occurred_at,
                booked_at=r.booked_at,
                transaction_type=r.transaction_type or "other",
                description=r.description,
                status=r.status or "pending",
                provider_fingerprint=r.provider_fingerprint,
            )
            for r in raw
        ]

    # ── Lifecycle helpers ──────────────────────────────────────────────

    async def _rate_limited_fetch_accounts(self) -> list[RawAccount]:
        """Call :meth:`fetch_accounts` with rate-limit and retry protection."""
        if self._rate_limiter is not None:
            result = await self._rate_limiter.retry(self.fetch_accounts)
            assert isinstance(result, list)
            return result  # type: ignore[return-value]
        return await self.fetch_accounts()

    async def _rate_limited_fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Call fetch_transactions with rate-limit + retry protection."""

        async def _fetch() -> object:
            return await self.fetch_transactions(
                since, account_id=account_id, limit=limit
            )

        if self._rate_limiter is not None:
            result = await self._rate_limiter.retry(_fetch)
            assert isinstance(result, list)
            return result  # type: ignore[return-value]
        return await self.fetch_transactions(
            since, account_id=account_id, limit=limit
        )

    # ── Introspection ──────────────────────────────────────────────────

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Return plugin metadata for discovery / UI."""
        # Resolve name — may be a class attribute or a property
        name_val = None
        name_attr = cls.__dict__.get("name", getattr(cls, "name", None))
        if not isinstance(name_attr, property):
            name_val = name_attr
        return {
            "name": name_val,
            "display_name": getattr(cls, "display_name", ""),
            "plugin_version": getattr(cls, "plugin_version", "0.1.0"),
            "has_rate_limit_policy": getattr(cls, "rate_limit_policy", None) is not None,
            "config_schema": getattr(cls, "config_schema", None),
        }


# ═══════════════════════════════════════════════════════════════════════
# ExporterPlugin
# ═══════════════════════════════════════════════════════════════════════


class ExporterPlugin(ABC):
    """Abstract base for a downstream export adapter plugin.

    Subclasses **must** override:

    * :meth:`export` — produce exported data in the desired format.
    * :attr:`name` — short exporter key (e.g. ``"actual_budget"``).

    The template method :meth:`run_export` handles setup, validation, and
    error handling; subclasses implement :meth:`export` for the actual
    data transformation.

    Lifecycle::

        exporter = MyExporterPlugin(config=...)
        result = await exporter.run_export(
            ExportRequest(format="csv", since=datetime(...))
        )
    """

    display_name: str = ""
    plugin_version: str = "0.1.0"
    config_schema: type[Any] | None = None

    #: List of supported format identifiers (see :class:`ExportFormat`).
    supported_formats: list[str] | None = None

    def __init__(self, config: object | None = None) -> None:
        self._config = config

    @property
    @abstractmethod
    def name(self) -> str:
        """Short unique exporter key, e.g. ``\"actual_budget\"``."""

    @abstractmethod
    async def export(self, request: ExportRequest) -> ExportResult:
        """Produce exported data for the given *request*.

        This is the main method subclasses implement.  It receives a
        fully-validated ``ExportRequest`` and must return an
        ``ExportResult``.

        The default :meth:`run_export` template method calls this after
        setting up any shared state.
        """

    # ── Template method ────────────────────────────────────────────────

    async def run_export(self, request: ExportRequest) -> ExportResult:
        """Template method that wraps :meth:`export` with setup/teardown.

        Subclasses may override this to add pre/post processing while
        keeping the core logic in :meth:`export`.  The default
        implementation simply delegates to :meth:`export`.
        """
        # Validate format support
        if (
            self.supported_formats is not None
            and request.format not in self.supported_formats
        ):
            return ExportResult(
                status="failed",
                error_message=(
                    f"Format {request.format!r} not supported by "
                    f"{self.name}. Supported: {self.supported_formats}"
                ),
            )

        return await self.export(request)

    # ── Introspection ──────────────────────────────────────────────────

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Return plugin metadata for discovery / UI."""
        return {
            "name": getattr(cls, "name", None),
            "display_name": getattr(cls, "display_name", ""),
            "plugin_version": getattr(cls, "plugin_version", "0.1.0"),
            "supported_formats": getattr(cls, "supported_formats", None),
            "config_schema": getattr(cls, "config_schema", None),
        }
