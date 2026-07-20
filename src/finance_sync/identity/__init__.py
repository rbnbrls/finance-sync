"""Identity Resolution — Pydantic models for the resolution pipeline."""

from datetime import datetime

from pydantic import BaseModel, Field


class ResolveRequest(BaseModel):
    """Manual resolution request from a human operator."""

    unresolved_security_id: str = Field(
        description="The unresolved security record to resolve"
    )
    target_security_id: str = Field(
        description="The canonical Security record to map to"
    )
    resolution_method: str = Field(
        default="manual",
        description="How was this resolved?  'manual', 'auto_isin', etc.",
    )
    resolution_notes: str | None = Field(
        default=None,
        description="Human-readable notes about the resolution decision",
    )


class MapRequest(BaseModel):
    """Request to link a specific incoming security to a canonical record."""

    provider_key: str = Field(description="Connector provider name")
    external_security_id: str = Field(
        description="Provider-local security / instrument ID"
    )
    target_security_id: str = Field(
        description="The canonical Security record to map to"
    )
    resolution_method: str = Field(
        default="manual",
        description="How was this resolved?",
    )
    resolution_notes: str | None = Field(
        default=None,
        description="Human-readable notes",
    )


class UnresolvedSecurityResponse(BaseModel):
    """API response for an unresolved security record."""

    id: str
    provider_key: str
    external_security_id: str
    raw_isin: str | None = None
    raw_figi: str | None = None
    raw_ticker: str | None = None
    raw_name: str | None = None
    raw_currency_code: str | None = None
    raw_metadata: str | None = None
    resolved_security_id: str | None = None
    resolution_method: str | None = None
    resolution_notes: str | None = None
    created_at: datetime
    updated_at: datetime


class AuditLogResponse(BaseModel):
    """API response for a resolution audit log entry."""

    id: str
    unresolved_security_id: str | None = None
    source_security_id: str | None = None
    target_security_id: str
    resolution_method: str
    confidence: str
    resolver_principal: str
    resolved_at: datetime
    resolution_detail: str | None = None
    match_score: float | None = None
    created_at: datetime


class ResolutionPipelineResult(BaseModel):
    """Result of running the identity resolution pipeline."""

    total_input: int = Field(
        description="Total securities passed to the pipeline"
    )
    resolved_auto: int = Field(
        description="Resolved automatically (ISIN / FIGI / ticker)"
    )
    resolved_fuzzy: int = Field(description="Resolved via fuzzy name matching")
    unresolved: int = Field(description="Remaining unresolved after all stages")
    audit_entries: int = Field(
        description="Audit log entries created during this run"
    )
