"""Pydantic models for the connector SDK.

Two tiers of data model:

1. **Raw** — provider-native DTOs.  These are the direct output of
   ``fetch_accounts()`` / ``fetch_transactions()`` and preserve the
   provider's original shape in ``provider_metadata``.

2. **Canonical** — normalised, provider-agnostic models that map to the
   SQLAlchemy ORM models in ``finance_sync.models``.  Connectors'
   ``transform()`` methods return lists of canonical models.
"""

from __future__ import annotations

from datetime import (
    datetime,
)
from decimal import (
    Decimal,
)
from typing import Any

from pydantic import BaseModel, Field


class ProviderMetadata(BaseModel):
    """Versioned, privacy-filtered provider extensions."""

    schema_version: str = "1"
    source_object_type: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class RawCashBalance(BaseModel):
    """Point-in-time provider cash balance in one currency."""

    amount: Decimal
    currency_code: str = Field(max_length=3)
    balance_kind: str = "available"
    observed_at: datetime


class SourceReference(BaseModel):
    """Relation to one or more provider object revisions."""

    object_type: str
    external_ids: list[str] = Field(default_factory=list)
    provider_revisions: list[str] = Field(default_factory=list)


class CategorySuggestion(BaseModel):
    value: str
    source: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    taxonomy: str | None = None


class TransactionSplitData(BaseModel):
    amount: Decimal
    currency_code: str
    percentage: Decimal | None = None
    category_suggestion: CategorySuggestion | None = None
    destination: str | None = None
    provenance: str = "user"


class TransactionAnnotationData(BaseModel):
    annotation_type: str
    content_hash: str | None = None
    mime_type: str | None = None
    safe_reference: str | None = None
    owner: str | None = None
    retention_until: datetime | None = None
    destination_reference: str | None = None


# ── Raw (provider-native) models ────────────────────────────────────────


class RawAccount(BaseModel):
    """Raw account data as returned by a provider.

    The provider SDK's own deserialised DTO should populate this.  Anything
    that doesn't fit the standard fields goes into ``provider_metadata``.
    """

    external_account_id: str = Field(
        description="Provider's unique identifier for this account"
    )
    name: str = Field(description="Human-readable account name")
    account_type: str = Field(
        description="Provider-native type, e.g. 'checking', 'savings', "
        "'brokerage', 'credit', 'loan', 'investment'"
    )
    account_subtype: str | None = Field(
        default=None, description="Provider-native subtype, e.g. '401k', '529'"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    current_balance: Decimal | None = Field(
        default=None, description="Current balance as reported by provider"
    )
    available_balance: Decimal | None = Field(
        default=None, description="Available balance (may differ from current)"
    )
    net_asset_value: Decimal | None = Field(
        default=None, description="Total account value including investments"
    )
    cash_balances: list[RawCashBalance] = Field(default_factory=list)
    iso_currency_code: str | None = Field(
        default=None,
        description="ISO-4217 code for the balance values, if different "
        "from currency_code",
    )
    provider_metadata: dict[str, Any] | None = Field(
        default=None,
        description="Provider-specific attributes that don't fit the "
        "standard schema",
    )
    capabilities: dict[str, bool] | None = Field(default=None)


class RawTransaction(BaseModel):
    """Raw transaction data as returned by a provider."""

    external_transaction_id: str = Field(
        description="Provider's unique identifier for this transaction"
    )
    external_account_id: str = Field(
        description="Provider account ID this transaction belongs to"
    )
    amount: Decimal = Field(
        description="Signed amount (positive = inflow, negative = outflow)"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    occurred_at: datetime = Field(
        description="When the transaction actually occurred (provider time)"
    )
    booked_at: datetime | None = Field(
        default=None,
        description="When the provider booked / settled the transaction",
    )
    description: str | None = Field(default=None)
    transaction_type: str | None = Field(
        default=None,
        description="Provider-native type, e.g. 'payment', 'purchase', "
        "'transfer', 'fee', 'interest', 'dividend'",
    )
    status: str | None = Field(
        default=None,
        description="Provider-native status, e.g. 'pending', 'booked', "
        "'cancelled'",
    )
    provider_fingerprint: str | None = Field(
        default=None,
        description="Provider-side checksum / hash for deduplication",
    )
    provider_metadata: dict[str, Any] | None = Field(
        default=None,
        description="Provider-specific attributes that don't fit the "
        "standard schema",
    )

    quantity: Decimal | None = Field(
        default=None,
        description="Number of units / shares transacted (for purchase/sale)",
    )
    unit_price: Decimal | None = Field(
        default=None,
        description="Provider-reported unit price in the instrument currency",
    )
    fee_amount: Decimal | None = Field(
        default=None,
        description="Provider-reported fee, stored as a positive amount",
    )
    fee_currency_code: str | None = Field(default=None, max_length=3)
    security_reference: SecurityReference | None = Field(
        default=None,
        description="Provider-neutral identity of the traded instrument",
    )
    amount_in_base: Decimal | None = Field(default=None)
    base_currency_code: str | None = Field(default=None)
    fx_rate: Decimal | None = Field(default=None)
    provider_metadata_contract: ProviderMetadata | None = None
    merchant_name: str | None = None
    merchant_id: str | None = None
    merchant_city: str | None = None
    merchant_country: str | None = None
    counterparty_name: str | None = None
    counterparty_account_reference: str | None = None
    merchant_category_code: str | None = None
    original_type: str | None = None
    original_status: str | None = None
    authorization_status: str | None = None
    settlement_status: str | None = None
    source_record_hash: str | None = None
    cashflow_bucket: str | None = None
    cashflow_suggestion: CategorySuggestion | None = None
    classification_source: str | None = None
    classification_override: str | None = None
    gross_amount: Decimal | None = None
    gross_currency_code: str | None = None
    net_amount: Decimal | None = None
    net_currency_code: str | None = None
    tax_amount: Decimal | None = None
    tax_currency_code: str | None = None
    refund_amount: Decimal | None = None
    refund_currency_code: str | None = None
    source_references: list[SourceReference] = Field(
        default_factory=lambda: list[SourceReference]()
    )


class SecurityReference(BaseModel):
    """Provider-supplied instrument identity; never contains database IDs."""

    external_id: str | None = Field(default=None)
    isin: str | None = Field(default=None, max_length=12)
    figi: str | None = Field(default=None, max_length=16)
    ticker: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None)
    venue: str | None = Field(default=None)
    currency_code: str | None = Field(default=None, max_length=3)
    security_type: str | None = Field(default=None)
    provider_metadata: dict[str, Any] | None = Field(default=None)

    def provider_identifier(self) -> str | None:
        """Return the best stable provider-local identifier."""
        return self.external_id or self.isin or self.figi or self.ticker


class RawHolding(BaseModel):
    """Raw point-in-time position snapshot returned by a provider."""

    external_account_id: str
    observed_at: datetime
    quantity: Decimal
    security_reference: SecurityReference
    cost_basis: Decimal | None = None
    cost_basis_currency: str | None = Field(default=None, max_length=3)
    market_value: Decimal | None = None
    currency_code: str = Field(default="EUR", max_length=3)
    price: Decimal | None = None
    price_currency: str | None = Field(default=None, max_length=3)
    provider_metadata: dict[str, Any] | None = None


# ── Canonical (normalised) models ───────────────────────────────────────

# These mirror the SQLAlchemy ORM models in finance_sync.models but are
# plain Pydantic so that connectors stay I/O-free.


class CanonicalAccountData(BaseModel):
    """Normalised, provider-agnostic account ready for upsert.

    Maps to the ``accounts`` table.
    """

    provider_key: str = Field(
        description="Connector name, e.g. 'bunq', 'trading212'"
    )
    external_account_id: str = Field(
        description="Provider's unique identifier for this account"
    )
    name: str = Field(description="Human-readable account name")
    account_type: str = Field(
        description="Normalised type: checking/savings/brokerage/"
        "credit/loan/investment"
    )
    account_subtype: str | None = Field(default=None)
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    current_balance: Decimal | None = Field(default=None)
    available_balance: Decimal | None = Field(default=None)
    net_asset_value: Decimal | None = Field(default=None)
    cash_balances: list[RawCashBalance] = Field(default_factory=list)
    iso_currency_code: str | None = Field(default=None)
    provider_metadata: dict[str, Any] | None = Field(default=None)
    is_active: bool = Field(default=True)
    capabilities: dict[str, bool] | None = Field(default=None)


class CanonicalTransactionData(BaseModel):
    """Normalised, provider-agnostic transaction ready for upsert.

    Maps to the ``transactions`` table.
    """

    provider_key: str = Field(
        description="Connector name, e.g. 'bunq', 'trading212'"
    )
    external_transaction_id: str = Field(
        description="Provider's unique transaction ID"
    )
    external_account_id: str = Field(
        description="Provider account ID this transaction belongs to"
    )
    amount: Decimal = Field(
        description="Signed amount (positive = inflow, negative = outflow)"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    occurred_at: datetime = Field(
        description="When the transaction actually occurred"
    )
    booked_at: datetime | None = Field(default=None)
    transaction_type: str = Field(
        description="Normalised type: transfer/payment/purchase/sale/fee/"
        "interest/dividend/withdrawal/deposit/other"
    )
    description: str | None = Field(default=None)
    quantity: Decimal | None = Field(
        default=None,
        description="Number of units / shares transacted (for purchase/sale)",
    )
    unit_price: Decimal | None = Field(default=None)
    fee_amount: Decimal | None = Field(default=None)
    fee_currency_code: str | None = Field(default=None, max_length=3)
    provider_metadata: dict[str, Any] | None = Field(default=None)
    status: str = Field(
        default="pending",
        description="pending/booked/reversed/cancelled",
    )
    provider_fingerprint: str | None = Field(default=None)
    security_reference: SecurityReference | None = Field(default=None)
    amount_in_base: Decimal | None = Field(default=None)
    base_currency_code: str | None = Field(default=None)
    fx_rate: Decimal | None = Field(default=None)
    provider_metadata_contract: ProviderMetadata | None = None
    merchant_name: str | None = None
    merchant_id: str | None = None
    merchant_city: str | None = None
    merchant_country: str | None = None
    counterparty_name: str | None = None
    counterparty_account_reference: str | None = None
    merchant_category_code: str | None = None
    original_type: str | None = None
    original_status: str | None = None
    authorization_status: str | None = None
    settlement_status: str | None = None
    source_record_hash: str | None = None
    cashflow_bucket: str | None = None
    cashflow_suggestion: CategorySuggestion | None = None
    classification_source: str | None = None
    classification_override: str | None = None
    gross_amount: Decimal | None = None
    gross_currency_code: str | None = None
    net_amount: Decimal | None = None
    net_currency_code: str | None = None
    tax_amount: Decimal | None = None
    tax_currency_code: str | None = None
    refund_amount: Decimal | None = None
    refund_currency_code: str | None = None
    source_references: list[SourceReference] = Field(
        default_factory=lambda: list[SourceReference]()
    )
    splits: list[TransactionSplitData] = Field(
        default_factory=lambda: list[TransactionSplitData]()
    )
    annotations: list[TransactionAnnotationData] = Field(
        default_factory=lambda: list[TransactionAnnotationData]()
    )


class CanonicalHoldingData(BaseModel):
    """Normalised, provider-agnostic holding snapshot ready for upsert."""

    provider_key: str
    external_account_id: str
    observed_at: datetime
    quantity: Decimal
    security_reference: SecurityReference
    cost_basis: Decimal | None = None
    cost_basis_currency: str | None = Field(default=None, max_length=3)
    market_value: Decimal | None = None
    currency_code: str = Field(default="EUR", max_length=3)
    price: Decimal | None = None
    price_currency: str | None = Field(default=None, max_length=3)
    source: str = Field(default="provider_sync")


class RawScheduledPayment(BaseModel):
    """Raw scheduled/recurring payment data as returned by a provider."""

    external_schedule_id: str = Field(
        description="Provider's unique identifier for this schedule"
    )
    external_account_id: str = Field(
        description="Provider account ID this schedule belongs to"
    )
    amount: Decimal = Field(
        description="Signed amount (positive = inflow, negative = outflow)"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    frequency: str = Field(
        description="Provider-native frequency description, "
        "e.g. 'WEEKLY', 'MONTHLY'"
    )
    interval: int | None = Field(
        default=None,
        description="Every N units of frequency (e.g. 2 for every 2 weeks)",
    )
    next_execution_date: datetime | None = Field(
        default=None, description="Next scheduled execution date"
    )
    end_date: datetime | None = Field(
        default=None, description="Schedule end date"
    )
    max_executions: int | None = Field(
        default=None, description="Maximum number of executions"
    )
    execution_count: int | None = Field(
        default=None, description="Times executed so far"
    )
    counterparty_name: str | None = Field(default=None)
    counterparty_iban: str | None = Field(default=None)
    description: str | None = Field(default=None)
    status: str | None = Field(
        default=None,
        description="Provider-native status, "
        "e.g. 'ACTIVE', 'PAUSED', 'CANCELLED'",
    )
    provider_metadata: dict[str, Any] | None = Field(default=None)


class RawCardTransaction(BaseModel):
    """Raw card transaction data as returned by a provider."""

    external_card_transaction_id: str = Field(
        description="Provider's unique card transaction identifier"
    )
    external_account_id: str = Field(
        description="Provider account ID this card belongs to"
    )
    amount: Decimal = Field(
        description="Signed amount (positive = inflow, negative = outflow)"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    merchant_name: str | None = Field(default=None)
    merchant_city: str | None = Field(default=None)
    merchant_country: str | None = Field(default=None)
    mcc: str | None = Field(default=None, description="Merchant Category Code")
    card_id: str | None = Field(
        default=None, description="Provider card identifier"
    )
    card_type: str | None = Field(
        default=None, description="debit/credit/prepaid/virtual"
    )
    card_last_four: str | None = Field(
        default=None, description="Last four digits of card PAN"
    )
    occurred_at: datetime = Field(
        description="When the transaction occurred (provider time)"
    )
    booked_at: datetime | None = Field(default=None)
    authorization_type: str | None = Field(
        default=None,
        description="Provider-native type: "
        "authorization/settlement/refund/chargeback",
    )
    description: str | None = Field(default=None)
    status: str | None = Field(
        default=None,
        description="Provider-native status, "
        "e.g. 'PENDING', 'BOOKED', 'REVERSED'",
    )
    provider_metadata: dict[str, Any] | None = Field(default=None)
    provider_metadata_contract: ProviderMetadata | None = None
    merchant_id: str | None = None
    merchant_category_code: str | None = None
    original_status: str | None = None
    authorization_status: str | None = None
    settlement_status: str | None = None
    source_record_hash: str | None = None
    refund_amount: Decimal | None = None
    refund_currency_code: str | None = None
    source_references: list[SourceReference] = Field(
        default_factory=lambda: list[SourceReference]()
    )


class CanonicalScheduledPaymentData(BaseModel):
    """Normalised, provider-agnostic scheduled payment ready for upsert.

    Maps to the ``scheduled_payments`` table.
    """

    provider_key: str = Field(
        description="Connector name, e.g. 'bunq', 'trading212'"
    )
    external_schedule_id: str = Field(
        description="Provider's unique schedule ID"
    )
    external_account_id: str = Field(
        description="Provider account ID this schedule belongs to"
    )
    amount: Decimal = Field(
        description="Signed amount (positive = inflow, negative = outflow)"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    frequency: str = Field(
        description="Normalised frequency: daily/weekly/biweekly/monthly/"
        "bimonthly/quarterly/semi_annually/annually/custom"
    )
    interval: int | None = Field(default=None)
    next_execution_date: datetime | None = Field(default=None)
    end_date: datetime | None = Field(default=None)
    max_executions: int | None = Field(default=None)
    execution_count: int = Field(default=0)
    counterparty_name: str | None = Field(default=None)
    counterparty_iban: str | None = Field(default=None)
    description: str | None = Field(default=None)
    status: str = Field(
        default="active",
        description="active/paused/completed/cancelled/failed",
    )


class CanonicalCardTransactionData(BaseModel):
    """Normalised, provider-agnostic card transaction ready for upsert.

    Maps to the ``card_transactions`` table.
    """

    provider_key: str = Field(
        description="Connector name, e.g. 'bunq', 'trading212'"
    )
    external_card_transaction_id: str = Field(
        description="Provider's unique card transaction ID"
    )
    external_account_id: str = Field(
        description="Provider account ID this card belongs to"
    )
    amount: Decimal = Field(
        description="Signed amount (positive = inflow, negative = outflow)"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    merchant_name: str | None = Field(default=None)
    merchant_city: str | None = Field(default=None)
    merchant_country: str | None = Field(default=None)
    mcc: str | None = Field(default=None)
    card_id: str | None = Field(default=None)
    card_type: str | None = Field(default=None)
    card_last_four: str | None = Field(default=None)
    occurred_at: datetime = Field(description="When the transaction occurred")
    booked_at: datetime | None = Field(default=None)
    authorization_type: str = Field(
        default="authorization",
        description="authorization/settlement/refund/chargeback/other",
    )
    description: str | None = Field(default=None)
    status: str = Field(
        default="pending",
        description="pending/booked/reversed/cancelled",
    )
    provider_metadata: dict[str, Any] | None = None
    provider_metadata_contract: ProviderMetadata | None = None
    merchant_id: str | None = None
    merchant_category_code: str | None = None
    original_status: str | None = None
    authorization_status: str | None = None
    settlement_status: str | None = None
    source_record_hash: str | None = None
    refund_amount: Decimal | None = None
    refund_currency_code: str | None = None
    source_references: list[SourceReference] = Field(
        default_factory=lambda: list[SourceReference]()
    )


# ── Configuration models ────────────────────────────────────────────────


class ConnectorConfig(BaseModel):
    """Configuration payload for instantiating a connector.

    ``credentials`` holds the provider-specific secrets (API keys, tokens,
    client IDs).  These are envelope-encrypted at rest and decrypted just
    before being handed to the connector.

    ``options`` holds non-secret configuration such as sandbox mode,
    custom endpoints, or feature toggles.

    ``connection_id`` / ``selected_accounts`` carry the connection
    context of the run: the stable credential id the config belongs to
    and the provider account ids selected for that connection (``None``/
    empty = sync all accounts the provider offers).  Sync callers pass
    them explicitly to the orchestrator, which prefers explicit kwargs
    over the config fields for backward compatibility.
    """

    provider_type: str = Field(
        description="Connector identifier, e.g. 'bunq', 'trading212'"
    )
    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="Provider-specific secrets (API key, client secret, …), "
        "decrypted from the credential store",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Non-secret configuration (sandbox mode, custom "
        "endpoints, feature toggles, …)",
    )
    connection_id: str | None = Field(
        default=None,
        description=(
            "Stable connection (credential) id this config belongs to; "
            "scopes sync persistence so same-provider connections never "
            "collide"
        ),
    )
    selected_accounts: list[str] | None = Field(
        default=None,
        description=(
            "Provider account ids selected for this connection; NULL/empty "
            "means 'sync all accounts the provider offers'"
        ),
    )


class ConnectorHealth(BaseModel):
    """Result of a connector health / connectivity check."""

    healthy: bool = Field(description="Whether the connector is operational")
    message: str | None = Field(
        default=None, description="Human-readable status or error message"
    )
    provider_type: str = Field(description="Connector identifier, e.g. 'bunq'")


# Rebuild models to resolve forward references caused by
# ``from __future__ import annotations`` with Pydantic v2.
RawAccount.model_rebuild()
RawTransaction.model_rebuild()
CanonicalAccountData.model_rebuild()
CanonicalTransactionData.model_rebuild()
RawScheduledPayment.model_rebuild()
RawCardTransaction.model_rebuild()
CanonicalScheduledPaymentData.model_rebuild()
CanonicalCardTransactionData.model_rebuild()
