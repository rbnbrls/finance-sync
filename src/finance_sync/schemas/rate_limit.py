"""Safe, actionable rate-limit diagnosis exposed by connection APIs."""

from datetime import datetime

from pydantic import BaseModel


class RateLimitDiagnosis(BaseModel):
    active: bool = False
    limited_at: datetime | None = None
    retry_after_at: datetime | None = None
    attempt_count: int = 0
    limit_scope: str | None = None
    last_http_status: int | None = None
    action: str | None = None
