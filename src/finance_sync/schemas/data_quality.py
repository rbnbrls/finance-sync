"""Public contract for tenant-scoped data-quality projections."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from finance_sync.schemas.control_plane import ControlPlaneAction

DataQualityKind = Literal[
    "duplicate_transaction",
    "missing_transaction",
    "cross_connector_mismatch",
    "amount_mismatch",
]


class DataQualityCoverage(BaseModel):
    provider: str
    resource: str = "transactions"
    accounts: int = 0
    transactions: int = 0
    first_transaction_at: datetime | None = None
    last_transaction_at: datetime | None = None


class DataQualityIssue(BaseModel):
    id: str
    kind: DataQualityKind
    severity: Literal["info", "warning", "error"]
    title: str
    description: str
    provider: str | None = None
    other_provider: str | None = None
    account_id: str | None = None
    transaction_ids: list[str] = Field(default_factory=list)
    external_record_ids: list[str] = Field(default_factory=list)
    impact_count: int = 0
    action: ControlPlaneAction


class DataQualityOverview(BaseModel):
    status: Literal["healthy", "attention_required", "unavailable"]
    latest_run_id: str | None = None
    latest_run_status: str | None = None
    latest_run_at: datetime | None = None
    findings_total: int = 0
    findings_by_kind: dict[str, int] = Field(default_factory=dict)
    findings_by_severity: dict[str, int] = Field(default_factory=dict)
    coverage: list[DataQualityCoverage] = Field(
        default_factory=list[DataQualityCoverage]
    )
    issues: list[DataQualityIssue] = Field(
        default_factory=list[DataQualityIssue]
    )
    generated_at: datetime
