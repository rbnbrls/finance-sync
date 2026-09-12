"""API DTOs for data-quality remediation operations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

RemediationStatus = Literal[
    "pending",
    "processing",
    "deferred",
    "retry_wait",
    "resolved",
    "failed",
    "ignored",
    "manual_review",
]


class RemediationItemResponse(BaseModel):
    id: str
    provider_key: str
    connection_id: str | None = None
    issue_type: str
    affected_entity_type: str
    affected_entity_id: str
    severity: str
    priority: int
    status: RemediationStatus
    remediation_strategy: str
    first_detected_at: datetime
    last_seen_at: datetime
    next_attempt_at: datetime
    attempt_count: int
    rate_limit_deferral_count: int
    context: dict[str, object] = Field(default_factory=dict)
    last_error: str | None = None
    last_error_category: str | None = None
    resolved_at: datetime | None = None
    verification_count: int

    model_config = {"from_attributes": True}

    @field_validator("id", "connection_id", mode="before")
    @classmethod
    def _stringify_uuid_fields(cls, value: object) -> object:
        """Keep the public API string-shaped for UUID-backed columns."""
        return None if value is None else str(value)


class RemediationListResponse(BaseModel):
    items: list[RemediationItemResponse]
    limit: int
    offset: int


class RemediationIgnoreRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=512)


class RemediationPriorityPatch(BaseModel):
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    status: Literal["pending", "manual_review", "ignored"] | None = None


class RemediationEnqueueResponse(BaseModel):
    enqueued: int
    item_ids: list[str]


class RemediationBulkRetryRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=100)
