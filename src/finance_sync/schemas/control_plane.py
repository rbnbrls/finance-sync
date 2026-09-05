"""Public contract for the read-only control-plane overview."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "error"]
OverviewStatus = Literal[
    "healthy", "attention_required", "sync_failed", "partial"
]
FreshnessStatus = Literal["fresh", "stale", "partial", "unavailable"]


def _empty_candidates() -> list[dict[str, Any]]:
    return []


class ControlPlaneAction(BaseModel):
    """A concrete, safe next step exposed by an issue."""

    key: str = "view_data_source"
    label: str
    method: Literal["GET", "POST", "PUT", "PATCH"]
    path: str
    permission: str = "enrichment:read"
    destructive: bool = False
    enabled: bool = True
    disabled_reason: str | None = None


class ControlPlaneIssue(BaseModel):
    """An actionable problem derived from existing domain state."""

    id: str
    severity: Severity
    category: str
    title: str
    description: str
    action: ControlPlaneAction
    provider: str | None = None
    external_record_id: str | None = None
    impact_count: int = 0
    candidate_securities: list[dict[str, Any]] = Field(
        default_factory=_empty_candidates
    )
    confidence: str | None = None
    affected_transaction_ids: list[str] = Field(default_factory=list)


class InstallationStatus(BaseModel):
    database: Literal["available"] = "available"
    redis: Literal["configured", "not_configured"]


class ControlPlaneSummary(BaseModel):
    connections_total: int = 0
    connections_healthy: int = 0
    syncs_failed: int = 0
    issues_open: int = 0
    destinations_failed: int = 0


class ControlPlaneConnection(BaseModel):
    id: str
    provider: str
    name: str
    status: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_category: str | None = None
    last_test_at: datetime | None = None
    last_test_status: str | None = None
    last_test_error: str | None = None
    next_scheduled_at: datetime | None = None
    actions: list[ControlPlaneAction] = Field(
        default_factory=lambda: list[ControlPlaneAction]()
    )


class ControlPlaneSync(BaseModel):
    id: str
    connector: str
    connection_id: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    items_processed: int | None = None
    error_message: str | None = None
    error_category: str | None = None
    cursor: datetime | None = None
    actions: list[ControlPlaneAction] = Field(
        default_factory=lambda: list[ControlPlaneAction]()
    )


class ControlPlaneFreshness(BaseModel):
    status: FreshnessStatus
    securities_total: int = 0
    securities_fresh: int = 0
    securities_stale: int = 0
    securities_without_quote: int = 0
    holdings_without_valuation: int = 0
    by_source: dict[str, dict[str, int]] = Field(default_factory=dict)
    by_category: dict[str, dict[str, int]] = Field(default_factory=dict)
    ingestion_last_at: datetime | None = None
    market_data_last_at: datetime | None = None
    last_enrichment_at: datetime | None = None


class ControlPlaneCoverage(BaseModel):
    connections_with_data: int = 0
    connections_total: int = 0
    providers: list[str] = Field(default_factory=list)


class ControlPlaneDestination(BaseModel):
    id: str
    type: str
    name: str
    status: str
    health_status: str | None = None
    last_checked_at: datetime | None = None
    last_error: str | None = None
    last_export_error: str | None = None
    next_scheduled_at: datetime | None = None
    actions: list[ControlPlaneAction] = Field(
        default_factory=lambda: list[ControlPlaneAction]()
    )
    selected_account_ids: list[str] = Field(default_factory=list)
    last_export_status: str | None = None
    last_export_at: datetime | None = None
    failed_export_count: int = 0
    delivery_checkpoint: dict[str, Any] | None = None


class ControlPlaneOverview(BaseModel):
    status: OverviewStatus
    installation: InstallationStatus
    summary: ControlPlaneSummary
    connections: list[ControlPlaneConnection]
    syncs: list[ControlPlaneSync]
    issues: list[ControlPlaneIssue]
    freshness: ControlPlaneFreshness
    coverage: ControlPlaneCoverage
    destinations: list[ControlPlaneDestination]
    as_of: datetime | None = None
    generated_at: datetime
