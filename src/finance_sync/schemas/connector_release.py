"""Secret-free connector release API contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ReleaseStatus = Literal[
    "candidate", "certified", "enabled", "deprecated", "blocked", "rolled_back"
]


class ConnectorReleaseRequest(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    previous_version: str | None = None
    certification_status: str = "pending"
    certification_commit: str | None = None
    compatibility_status: str = "pending"
    canary_status: str = "pending"
    capabilities: list[str] = Field(default_factory=list)


class ConnectorReleaseResponse(BaseModel):
    id: str
    provider_key: str
    version: str
    status: ReleaseStatus
    previous_version: str | None
    certification_status: str
    certification_commit: str | None
    compatibility_status: str
    canary_status: str
    capabilities: list[str]
    reason_code: str | None
    enabled_at: datetime | None
    disabled_at: datetime | None
