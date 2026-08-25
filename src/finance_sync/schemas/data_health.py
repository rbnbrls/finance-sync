"""Canonical product contract for the tenant-scoped Data health view."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from finance_sync.schemas.control_plane import ControlPlaneAction

DataHealthStatus = Literal[
    "healthy", "attention_required", "error", "partial", "unavailable"
]
DataHealthCategory = Literal[
    "connection",
    "synchronization",
    "missing_transactions",
    "provider_data_changed",
    "balance_conflict",
    "stale_prices",
    "duplicate_accounts",
    "unresolved_security",
    "incomplete_import",
    "failed_export",
    "reconciliation",
    "security_mapping",
    "freshness",
    "destination",
    "export",
    "data_quality",
]


class DataHealthIssue(BaseModel):
    """One actionable problem shown on the Data health page."""

    id: str
    category: DataHealthCategory
    severity: Literal["info", "warning", "error"]
    title: str
    description: str
    impact_count: int = 0
    provider: str | None = None
    source: str | None = None
    action: ControlPlaneAction


class DataHealthSource(BaseModel):
    """Operational state and data coverage for one configured source."""

    id: str
    provider: str
    status: str
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    transactions: int = 0
    accounts: int = 0


class DataHealthReconciliation(BaseModel):
    findings_total: int = 0
    findings_by_kind: dict[str, int] = Field(default_factory=dict)
    latest_run_at: datetime | None = None


class DataHealthOverview(BaseModel):
    status: DataHealthStatus
    last_successful_sync: datetime | None = None
    sources: list[DataHealthSource] = Field(default_factory=list)
    stale_data: dict[str, int] = Field(default_factory=dict)
    unresolved_securities: int = 0
    failed_exports: int = 0
    reconciliation: DataHealthReconciliation = Field(
        default_factory=DataHealthReconciliation
    )
    issues: list[DataHealthIssue] = Field(default_factory=list)
    as_of: datetime | None = None
    generated_at: datetime
