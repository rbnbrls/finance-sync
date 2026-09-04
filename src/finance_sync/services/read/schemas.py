"""Response schemas for the read API."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed by Pydantic model fields
from decimal import Decimal

from pydantic import BaseModel, Field

from finance_sync.models.enums import (
    TransactionType,  # noqa: TC001 — needed by Pydantic model fields
)
from finance_sync.schemas.freshness import (
    AggregateMeta,
    CollectionMeta,
)

E = Decimal


class AccountSummary(BaseModel):
    id: str
    connection_id: str | None = None
    name: str
    account_type: str
    account_subtype: str | None = None
    currency_code: str
    current_balance: E | None = None
    available_balance: E | None = None
    net_asset_value: E | None = None
    provider_key: str
    is_active: bool
    owner_user_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    capabilities: dict[str, bool] | None = None


class AccountDetailResponse(BaseModel):
    items: list[AccountSummary]
    total: int
    limit: int
    offset: int


class TransactionResponse(BaseModel):
    id: str
    account_id: str
    security_id: str | None = None
    amount: E
    currency_code: str
    occurred_at: datetime
    booked_at: datetime | None = None
    description: str | None = None
    transaction_type: TransactionType
    status: str
    provider_key: str
    tombstoned_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    provider_metadata_contract: dict[str, object] | None = None
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
    cashflow_suggestion: dict[str, object] | None = None
    classification_source: str | None = None
    classification_override: str | None = None
    gross_amount: E | None = None
    gross_currency_code: str | None = None
    net_amount: E | None = None
    net_currency_code: str | None = None
    tax_amount: E | None = None
    tax_currency_code: str | None = None
    refund_amount: E | None = None
    refund_currency_code: str | None = None


class TransactionListResponse(BaseModel):
    items: list[TransactionResponse]
    total: int
    limit: int
    offset: int


class TopLevelTransactionListResponse(BaseModel):
    """Top-level ``GET /transactions`` response.

    Mirrors the account-scoped list but adds the ``meta`` envelope
    promised by ``docs/API.md`` for collection endpoints.
    """

    items: list[TransactionResponse]
    total: int
    limit: int
    offset: int
    meta: CollectionMeta = Field(
        default_factory=CollectionMeta,
        description=(
            "As-of / currency / cursor / freshness envelope "
            "(docs/API.md ``meta`` contract)"
        ),
    )


class DividendListResponse(BaseModel):
    """Top-level ``GET /dividends`` response.

    Dividend-type transactions across the tenant's accounts, with the
    collection ``meta`` envelope.
    """

    items: list[TransactionResponse]
    total: int
    limit: int
    offset: int
    meta: CollectionMeta = Field(
        default_factory=CollectionMeta,
        description=(
            "As-of / currency / cursor / freshness envelope "
            "(docs/API.md ``meta`` contract)"
        ),
    )


class ScheduledPaymentResponse(BaseModel):
    id: str
    account_id: str | None = None
    provider_key: str
    external_schedule_id: str
    amount: E
    currency_code: str
    amount_in_base: E | None = None
    frequency: str
    interval: int | None = None
    next_execution_date: datetime | None = None
    end_date: datetime | None = None
    max_executions: int | None = None
    execution_count: int = 0
    counterparty_name: str | None = None
    counterparty_iban: str | None = None
    description: str | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScheduledPaymentListResponse(BaseModel):
    items: list[ScheduledPaymentResponse]
    total: int
    limit: int
    offset: int


class CardTransactionResponse(BaseModel):
    id: str
    account_id: str | None = None
    provider_key: str
    external_card_transaction_id: str
    amount: E
    currency_code: str
    amount_in_base: E | None = None
    merchant_name: str | None = None
    merchant_city: str | None = None
    merchant_country: str | None = None
    mcc: str | None = None
    card_id: str | None = None
    card_type: str | None = None
    card_last_four: str | None = None
    occurred_at: datetime
    booked_at: datetime | None = None
    transaction_type: str
    authorization_type: str
    description: str | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    provider_metadata_contract: dict[str, object] | None = None
    merchant_id: str | None = None
    merchant_category_code: str | None = None
    original_status: str | None = None
    authorization_status: str | None = None
    settlement_status: str | None = None
    source_record_hash: str | None = None
    refund_amount: E | None = None
    refund_currency_code: str | None = None


class CardTransactionListResponse(BaseModel):
    items: list[CardTransactionResponse]
    total: int
    limit: int
    offset: int


class BalanceResponse(BaseModel):
    id: str
    account_id: str
    observed_at: datetime
    balance_kind: str
    amount: E
    currency_code: str
    source: str
    created_at: datetime | None = None


class BalanceListResponse(BaseModel):
    items: list[BalanceResponse]
    total: int
    limit: int
    offset: int


class SecurityInfo(BaseModel):
    id: str
    isin: str | None = None
    figi: str | None = None
    ticker: str | None = None
    name: str
    security_type: str
    currency_code: str
    latest_price: E | None = None
    latest_price_currency: str | None = None
    latest_price_timestamp: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SecurityListResponse(BaseModel):
    items: list[SecurityInfo]
    total: int
    limit: int
    offset: int


class SecurityPriceResponse(BaseModel):
    id: str
    security_id: str
    timestamp: datetime
    price_open: E | None = None
    price_high: E | None = None
    price_low: E | None = None
    price_close: E | None = None
    volume: E | None = None
    source: str
    interval: str
    currency_code: str


class SecurityPriceListResponse(BaseModel):
    items: list[SecurityPriceResponse]
    total: int
    limit: int
    offset: int


class TopLevelPriceListResponse(BaseModel):
    """Top-level ``GET /prices`` response.

    Either the price series for one security/listing or the latest
    price per security, with the collection ``meta`` envelope.
    """

    items: list[SecurityPriceResponse]
    total: int
    limit: int
    offset: int
    meta: CollectionMeta = Field(
        default_factory=CollectionMeta,
        description=(
            "As-of / currency / cursor / freshness envelope "
            "(docs/API.md ``meta`` contract)"
        ),
    )


class HoldingBreakdown(BaseModel):
    security_id: str
    ticker: str | None = None
    security_name: str
    security_type: str
    quantity: E
    cost_basis: E | None = None
    cost_basis_currency: str | None = None
    market_value: E | None = None
    price: E | None = None
    price_currency: str | None = None
    currency_code: str
    unrealised_pl: E | None = None
    unrealised_pl_pct: E | None = None


class AccountPortfolioBreakdown(BaseModel):
    account_id: str
    account_name: str
    account_type: str
    holdings: list[HoldingBreakdown]
    total_value: E | None = None
    total_cost_basis: E | None = None


class PortfolioResponse(BaseModel):
    accounts: list[AccountPortfolioBreakdown]
    total_value: E | None = None
    total_cost_basis: E | None = None
    currency_code: str = "EUR"


class HoldingItemResponse(BaseModel):
    """One aggregated current holding (latest snapshot per position)."""

    account_id: str
    account_name: str | None = None
    security_id: str
    ticker: str | None = None
    security_name: str
    security_type: str
    quantity: E
    cost_basis: E | None = None
    cost_basis_currency: str | None = None
    market_value: E | None = None
    price: E | None = None
    price_currency: str | None = None
    currency_code: str
    observed_at: datetime
    unrealised_pl: E | None = None
    unrealised_pl_pct: E | None = None


class HoldingsListResponse(BaseModel):
    """Top-level ``GET /holdings`` response with the collection ``meta``."""

    items: list[HoldingItemResponse]
    total: int
    limit: int
    offset: int
    meta: CollectionMeta = Field(
        default_factory=CollectionMeta,
        description=(
            "As-of / currency / cursor / freshness envelope "
            "(docs/API.md ``meta`` contract)"
        ),
    )


class PortfolioHistoryEntry(BaseModel):
    date: datetime
    total_value: E
    currency_code: str = "EUR"


class PortfolioHistoryResponse(BaseModel):
    items: list[PortfolioHistoryEntry]
    total: int
    limit: int
    offset: int


class NetWorthResponse(BaseModel):
    total_assets: E | None = None
    total_liabilities: E | None = None
    net_worth: E | None = None
    currency_code: str = "EUR"
    as_of: datetime | None = None
    accounts: list[AccountSummary] = Field(default_factory=list[AccountSummary])


class NetWorthHistoryEntry(BaseModel):
    date: datetime
    net_worth: E
    total_assets: E
    total_liabilities: E
    currency_code: str = "EUR"


class NetWorthHistoryResponse(BaseModel):
    items: list[NetWorthHistoryEntry]
    total: int
    limit: int
    offset: int


class CashflowResponse(BaseModel):
    """Cash flow summary for a given period.

    Uses transaction-level data: positive amounts = inflows,
    negative amounts = outflows.
    """

    total_inflows: E | None = None
    total_outflows: E | None = None
    net_cashflow: E | None = None
    transaction_count: int = 0
    currency_code: str = "EUR"
    period_start: datetime | None = None
    period_end: datetime | None = None
    meta: AggregateMeta = Field(
        default_factory=AggregateMeta,
        description=(
            "As-of / freshness / coverage envelope (docs/API.md "
            "``meta`` contract)"
        ),
    )


class CashflowHistoryEntry(BaseModel):
    """Single day's cash flow aggregated from transactions."""

    date: datetime
    inflows: E
    outflows: E
    net: E
    transaction_count: int = 0
    currency_code: str = "EUR"


class CashflowHistoryResponse(BaseModel):
    """Paginated time-series of cash flow data."""

    items: list[CashflowHistoryEntry]
    total: int
    limit: int
    offset: int
    period_start: datetime | None = None
    period_end: datetime | None = None
    meta: AggregateMeta = Field(
        default_factory=AggregateMeta,
        description=(
            "As-of / freshness / coverage envelope (docs/API.md "
            "``meta`` contract)"
        ),
    )


class SyncRunResponse(BaseModel):
    id: str
    connector: str
    connection_id: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    #: Watermark the run advanced the sync cursor to on success
    #: (NULL for failed runs); see the ``sync_cursor`` table for the
    #: per-resource resume positions.
    cursor: datetime | None = None
    items_processed: int | None = None
    error_message: str | None = None
    error_category: str | None = None
    warnings: list[str] = Field(default_factory=list)
    duration_seconds: float | None = None
    created_at: datetime | None = None


class SyncRunStatusCount(BaseModel):
    connector: str
    status: str
    count: int


class SyncRunListResponse(BaseModel):
    items: list[SyncRunResponse]
    status_counts: list[SyncRunStatusCount]
    total: int
    limit: int
    offset: int
