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
from typing import TYPE_CHECKING, ClassVar, cast

from finance_sync.connectors.exceptions import ConnectorError
from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalCardTransactionData,
    CanonicalHoldingData,
    CanonicalScheduledPaymentData,
    CanonicalTransactionData,
    ConnectorConfig,
    ConnectorHealth,
    RawAccount,
    RawCardTransaction,
    RawHolding,
    RawScheduledPayment,
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

    #: Semantic version of the connector implementation itself.  Older
    #: third-party connectors may omit this attribute and receive the safe
    #: default used by :class:`ConnectorRegistry`.
    plugin_version: str = "0.1.0"

    #: Optional, non-sensitive metadata for the public connector catalog.
    #: Only documented scalar/list fields are exposed by the registry.
    catalog_metadata: ClassVar[dict[str, object]] = {}

    #: Ways a user can ingest data for this connector.  API is the backwards-
    #: compatible default; file-based connectors override this explicitly.
    ingestion_methods: ClassVar[tuple[str, ...]] = ("api",)

    #: Optional secret-safe hints consumed by the import wizard.  Complex
    #: parsing and validation remain in the connector/API adapter.
    import_wizard: ClassVar[dict[str, object]] = {}

    #: Resources implemented by this connector.  Holdings are deliberately
    #: opt-in so existing third-party connectors keep their old behaviour.
    supported_resources: frozenset[str] = frozenset(
        {"accounts", "transactions"}
    )

    #: Optional capability -> availability contract.
    capabilities: ClassVar[dict[str, str]] = {}

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

    async def credential_expiry(self) -> datetime | None:
        """Return the credential expiry in UTC when the provider exposes it.

        Connectors without a provider expiry signal deliberately return
        ``None``; callers must report expiry as unknown rather than inventing
        a date.
        """
        return None

    async def reauthenticate(self) -> None:
        """Validate/refresh credentials for the reauthentication flow.

        The default keeps older connectors compatible by delegating to their
        existing authentication implementation.
        """
        await self.authenticate()

    async def fetch_holdings(
        self, *, account_id: str | None = None
    ) -> list[RawHolding]:
        """Return current or historical position snapshots.

        Connectors must also add ``"holdings"`` to
        :attr:`supported_resources`; the default is a no-op for backwards
        compatibility.
        """
        del account_id
        return []

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
                capabilities=r.capabilities,
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
                quantity=r.quantity,
                unit_price=r.unit_price,
                fee_amount=r.fee_amount,
                fee_currency_code=r.fee_currency_code,
                status=r.status or "pending",
                provider_metadata=r.provider_metadata,
                provider_fingerprint=r.provider_fingerprint,
                security_reference=r.security_reference,
                amount_in_base=r.amount_in_base,
                base_currency_code=r.base_currency_code,
                fx_rate=r.fx_rate,
                provider_metadata_contract=r.provider_metadata_contract,
                merchant_name=r.merchant_name,
                merchant_id=r.merchant_id,
                merchant_city=r.merchant_city,
                merchant_country=r.merchant_country,
                counterparty_name=r.counterparty_name,
                counterparty_account_reference=r.counterparty_account_reference,
                merchant_category_code=r.merchant_category_code,
                original_type=r.original_type or r.transaction_type,
                original_status=r.original_status or r.status,
                authorization_status=r.authorization_status,
                settlement_status=r.settlement_status,
                source_record_hash=r.source_record_hash,
                cashflow_bucket=r.cashflow_bucket,
                cashflow_suggestion=r.cashflow_suggestion,
                classification_source=r.classification_source,
                classification_override=r.classification_override,
                gross_amount=r.gross_amount,
                gross_currency_code=r.gross_currency_code,
                net_amount=r.net_amount,
                net_currency_code=r.net_currency_code,
                tax_amount=r.tax_amount,
                tax_currency_code=r.tax_currency_code,
                refund_amount=r.refund_amount,
                refund_currency_code=r.refund_currency_code,
                source_references=r.source_references,
            )
            for r in raw
        ]

    def transform_holdings(
        self, raw: list[RawHolding]
    ) -> list[CanonicalHoldingData]:
        """Transform provider holdings to canonical snapshots."""
        return [
            CanonicalHoldingData(
                provider_key=self.name,
                external_account_id=r.external_account_id,
                observed_at=r.observed_at,
                quantity=r.quantity,
                security_reference=r.security_reference,
                cost_basis=r.cost_basis,
                cost_basis_currency=r.cost_basis_currency,
                market_value=r.market_value,
                currency_code=r.currency_code,
                price=r.price,
                price_currency=r.price_currency,
            )
            for r in raw
        ]

    def transform_scheduled_payments(
        self,
        raw: list[RawScheduledPayment],
    ) -> list[CanonicalScheduledPaymentData]:
        """Transform raw scheduled payments to canonical form.

        The default implementation copies matching fields by name and
        normalises frequency to lowercase and status to the canonical
        ``active/paused/completed/cancelled/failed`` set.  Override for
        provider-specific normalisation.
        """
        return [
            CanonicalScheduledPaymentData(
                provider_key=self.name,
                external_schedule_id=r.external_schedule_id,
                external_account_id=r.external_account_id,
                amount=r.amount,
                currency_code=r.currency_code,
                frequency=r.frequency.lower(),
                interval=r.interval,
                next_execution_date=r.next_execution_date,
                end_date=r.end_date,
                max_executions=r.max_executions,
                execution_count=r.execution_count or 0,
                counterparty_name=r.counterparty_name,
                counterparty_iban=r.counterparty_iban,
                description=r.description,
                status=r.status or "active",
            )
            for r in raw
        ]

    def transform_card_transactions(
        self,
        raw: list[RawCardTransaction],
    ) -> list[CanonicalCardTransactionData]:
        """Transform raw card transactions to canonical form.

        The default implementation copies matching fields by name and
        normalises the authorization type and status to the canonical
        sets.  Override for provider-specific normalisation.
        """
        return [
            CanonicalCardTransactionData(
                provider_key=self.name,
                external_card_transaction_id=r.external_card_transaction_id,
                external_account_id=r.external_account_id,
                amount=r.amount,
                currency_code=r.currency_code,
                merchant_name=r.merchant_name,
                merchant_city=r.merchant_city,
                merchant_country=r.merchant_country,
                mcc=r.mcc,
                card_id=r.card_id,
                card_type=r.card_type,
                card_last_four=r.card_last_four,
                occurred_at=r.occurred_at,
                booked_at=r.booked_at,
                authorization_type=r.authorization_type or "authorization",
                description=r.description,
                status=r.status or "pending",
                provider_metadata_contract=r.provider_metadata_contract,
                merchant_id=r.merchant_id,
                merchant_category_code=r.merchant_category_code or r.mcc,
                original_status=r.original_status or r.status,
                authorization_status=r.authorization_status,
                settlement_status=r.settlement_status,
                source_record_hash=r.source_record_hash,
                refund_amount=r.refund_amount,
                refund_currency_code=r.refund_currency_code,
                source_references=r.source_references,
            )
            for r in raw
        ]

    # ── Lifecycle helpers ──────────────────────────────────────────────

    async def _rate_limited_fetch_accounts(self) -> list[RawAccount]:
        """Call :meth:`fetch_accounts` with rate-limit and retry protection."""
        if self._rate_limiter is not None:
            result = await self._rate_limiter.retry(self.fetch_accounts)
            assert isinstance(result, list)
            return cast("list[RawAccount]", result)
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
            return cast("list[RawTransaction]", result)
        return await self.fetch_transactions(
            since, account_id=account_id, limit=limit
        )

    async def _rate_limited_fetch_holdings(
        self, *, account_id: str | None = None
    ) -> list[RawHolding]:
        """Call :meth:`fetch_holdings` with rate-limit protection."""

        async def _fetch() -> object:
            return await self.fetch_holdings(account_id=account_id)

        if self._rate_limiter is not None:
            result = await self._rate_limiter.retry(_fetch)
            assert isinstance(result, list)
            return cast("list[RawHolding]", result)
        return await self.fetch_holdings(account_id=account_id)
