"""Connector abstract base class.

All financial-data connectors **must** subclass ``Connector`` and implement
its abstract methods.  Concrete classes are discovered at runtime via the
``finance_sync.connectors`` entry point group.

Credential lifecycle
--------------------
Credentials are envelope-encrypted (AES-256-GCM) at rest (see
:mod:`finance_sync.services.auth`).  The framework decrypts them
immediately before calling ``authenticate()`` and provides the
decrypted secrets in ``self.config.credentials``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from finance_sync.connectors.exceptions import ConnectorError
from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    ConnectorHealth,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.rate_limiter import RateLimiter, RateLimitPolicy

if TYPE_CHECKING:
    from datetime import datetime


class Connector(ABC):
    """Abstract base for a financial-data provider connector.

    Subclasses **must** override:

    * :meth:`authenticate` -- validate/refresh credentials.
    * :meth:`fetch_accounts` -- return raw provider accounts.
    * :meth:`fetch_transactions` -- return raw provider transactions.
    * :attr:`name` -- short provider key (e.g. ``"bunq"``).

    Subclasses **may** override:

    * :meth:`transform_accounts` / :meth:`transform_transactions` -- map
      raw data to canonical models (default implementation provides a
      best-effort identity mapping).
    * :meth:`health` -- lightweight connectivity check (default calls
      ``authenticate``).
    """

    #: Human-readable display name (defaults to ``name``).
    display_name: str = ""

    #: Semantic version of the connector SDK this connector targets.
    #: Must be a PEP 440 version string such as ``"0.1.0"``.
    sdk_version: str = "0.1.0"

    #: Optional rate-limit policy.  When set, the base class wraps
    #: ``fetch_accounts`` and ``fetch_transactions`` with rate-limited,
    #: auto-retrying variants.
    rate_limit_policy: RateLimitPolicy | None = None

    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config
        self._authenticated = False
        self._rate_limiter = (
            RateLimiter(self.rate_limit_policy)
            if self.rate_limit_policy
            else None
        )

    # ── Required overrides ─────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Short unique connector key, e.g. ``\"bunq\"`` or ``\"trading212\"``.

        Must match the :attr:`ConnectorConfig.provider_type` it is registered
        under.
        """

    @abstractmethod
    async def authenticate(self) -> None:
        """Obtain or validate provider credentials.

        Raise :class:`PermanentError` on invalid secrets.
        Raise :class:`TransientError` on temporary provider unavailability.

        Implementation must use ``self.config.credentials`` (already
        decrypted by the framework).
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

        Default implementation calls :meth:`authenticate` -- override for a
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
            return result
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
            return result
        return await self.fetch_transactions(
            since, account_id=account_id, limit=limit
        )
