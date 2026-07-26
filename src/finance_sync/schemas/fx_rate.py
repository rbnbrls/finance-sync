"""Pydantic schemas for the FxRate model.

Provides API-facing request/response schemas that mirror the
FxRate ORM model without a database dependency.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class FxRateCreate(BaseModel):
    """Schema for creating a new FxRate record."""

    base_currency: str = Field(
        min_length=3,
        max_length=3,
        description="ISO-4217 base currency code (e.g. 'EUR')",
    )
    quote_currency: str = Field(
        min_length=3,
        max_length=3,
        description="ISO-4217 quote currency code (e.g. 'USD')",
    )
    rate: Decimal = Field(
        description="Exchange rate (1 base_currency = rate quote_currency)",
    )
    timestamp: datetime = Field(
        description="When the rate observation was recorded",
    )
    source: str = Field(
        default="openbb",
        description="Data source identifier (e.g. 'openbb', 'ecb', 'manual')",
    )


class FxRateResponse(BaseModel):
    """Schema for returning an FxRate record."""

    id: UUID = Field(description="Unique identifier")
    base_currency: str = Field(
        description="ISO-4217 base currency code (e.g. 'EUR')",
    )
    quote_currency: str = Field(
        description="ISO-4217 quote currency code (e.g. 'USD')",
    )
    rate: Decimal = Field(
        description="Exchange rate (1 base_currency = rate quote_currency)",
    )
    timestamp: datetime = Field(
        description="When the rate observation was recorded",
    )
    source: str = Field(
        description="Data source identifier (e.g. 'openbb', 'ecb', 'manual')",
    )
    created_at: datetime = Field(description="Record creation timestamp")
    updated_at: datetime = Field(description="Record last-update timestamp")

    model_config = {"from_attributes": True}
