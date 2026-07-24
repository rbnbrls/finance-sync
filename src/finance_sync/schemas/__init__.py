"""API schemas for request/response serialization.

Schemas mirror the ORM models but are Pydantic-only, with no
database dependency. They support both serialization (model_dump)
and deserialization (model_validate) for API endpoints.
"""

from __future__ import annotations

from finance_sync.schemas.fx_rate import FxRateCreate, FxRateResponse

__all__ = [
    "FxRateCreate",
    "FxRateResponse",
]
