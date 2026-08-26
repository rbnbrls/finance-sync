"""Public contract for connection, source and processing health."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from finance_sync.schemas.connector_compatibility import ConnectorCompatibility
from finance_sync.schemas.rate_limit import RateLimitDiagnosis

ProviderHealthStatus = Literal[
    "healthy",
    "attention_required",
    "unavailable",
    "incompatible",
    "error",
    "paused",
]


class ProviderConnectionHealth(BaseModel):
    """Health of credentials and provider connectivity only."""

    status: str
    credential_status: str
    auth_status: str
    checked_at: datetime | None = None
    last_test_at: datetime | None = None
    error_code: str | None = None
    message: str | None = None
    rate_limit: RateLimitDiagnosis = Field(default_factory=RateLimitDiagnosis)
    expires_at: datetime | None = None
    reauth_required_at: datetime | None = None


class ProviderResourceHealth(BaseModel):
    """Health and freshness of one connector resource."""

    resource: str
    supported: bool = True
    source_status: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    fresh_until: datetime | None = None
    items_processed: int = 0
    sync_run_id: str | None = None
    error_category: str | None = None
    stale: bool = True


class ProviderProcessingHealth(BaseModel):
    """Summary of the latest successful processing for a connection."""

    last_success_at: datetime | None = None
    sync_run_id: str | None = None
    resources_processed: int = 0
    all_supported_resources_processed: bool = False


class ProviderHealthOverview(BaseModel):
    """Canonical three-level provider health projection."""

    connection_id: str
    provider: str
    overall_status: ProviderHealthStatus
    connection: ProviderConnectionHealth
    resources: list[ProviderResourceHealth] = Field(
        default_factory=lambda: list[ProviderResourceHealth]()
    )
    last_successful_processing: ProviderProcessingHealth
    compatibility: ConnectorCompatibility
    action_required: str | None = None
    evaluated_at: datetime
