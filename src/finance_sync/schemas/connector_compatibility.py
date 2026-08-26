"""Public connector compatibility response contract."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ConnectorCompatibility(BaseModel):
    """Safe lifecycle result for one connector installation."""

    provider_key: str
    status: str = Field(description="Normalised lifecycle status")
    reason: str = Field(description="Stable, non-sensitive reason code")
    current_version: str | None = None
    previous_version: str | None = None
    minimum_fixture_version: str | None = None
    certification_status: str = "unknown"
    certified_at: date | None = None
    certification_commit: str | None = None
    deprecation_date: date | None = None
    removal_date: date | None = None
    migration_required: bool = False
    warnings: list[str] = Field(default_factory=list)
