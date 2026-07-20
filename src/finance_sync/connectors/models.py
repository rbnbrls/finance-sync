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

from datetime import datetime  # noqa: TC003 — needed by model_rebuild()
from decimal import Decimal  # noqa: TC003 — needed by model_rebuild()
from typing import Any

from pydantic import BaseModel, Field

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
    iso_currency_code: str | None = Field(default=None)
    provider_metadata: dict[str, Any] | None = Field(default=None)
    is_active: bool = Field(default=True)


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
    status: str = Field(
        default="pending",
        description="pending/booked/reversed/cancelled",
    )
    provider_fingerprint: str | None = Field(default=None)


# ── Configuration models ────────────────────────────────────────────────


class ConnectorConfig(BaseModel):
    """Configuration payload for instantiating a connector.

    ``credentials`` holds the provider-specific secrets (API keys, tokens,
    client IDs).  These are envelope-encrypted at rest and decrypted just
    before being handed to the connector.

    ``options`` holds non-secret configuration such as sandbox mode,
    custom endpoints, or feature toggles.
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
