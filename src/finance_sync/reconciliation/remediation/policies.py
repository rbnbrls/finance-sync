"""Shared retry/error policy for remediation workers."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)

SEVERITY_WEIGHTS = {
    "critical": 400,
    "error": 300,
    "warning": 200,
    "info": 100,
}


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    category: str
    status: str


def scheduling_score(item: object, *, now: datetime | None = None) -> float:
    """Return the bounded, explainable score used for due-work ordering."""
    current = now or datetime.now(UTC)
    severity = str(getattr(item, "severity", "warning")).lower()
    priority = _bounded_int(getattr(item, "priority", 0), -1000, 1000)
    attempts = _bounded_int(getattr(item, "attempt_count", 0), 0, 10)
    first_detected = getattr(item, "first_detected_at", current)
    if isinstance(first_detected, datetime):
        if first_detected.tzinfo is None:
            first_detected = first_detected.replace(tzinfo=UTC)
        age_hours = max(
            0.0,
            (current - first_detected.astimezone(UTC)).total_seconds() / 3600,
        )
    else:
        age_hours = 0.0
    context = getattr(item, "context", {})
    cost = 0
    if isinstance(context, dict):
        typed_context = cast("dict[str, Any]", context)
        cost = _bounded_int(typed_context.get("request_cost", 0), 0, 100)
    return (
        float(SEVERITY_WEIGHTS.get(severity, SEVERITY_WEIGHTS["warning"]))
        + priority
        + min(age_hours / 24.0, 30.0)
        - attempts
        - cost
    )


def _bounded_int(value: object, lower: int, upper: int) -> int:
    try:
        return max(lower, min(int(cast("Any", value)), upper))
    except (TypeError, ValueError):
        return lower


def classify_error(exc: Exception) -> ErrorClassification:
    if isinstance(exc, RateLimitError):
        return ErrorClassification("rate_limit", "deferred")
    if isinstance(exc, (TransientError, TimeoutError, ConnectionError)):
        return ErrorClassification("transient", "retry_wait")
    if isinstance(exc, PermanentError):
        message = str(exc).lower()
        if any(
            marker in message
            for marker in (
                "auth",
                "credential",
                "access token",
                "api key",
                "401",
                "reauth",
            )
        ):
            return ErrorClassification("authentication", "manual_review")
        return ErrorClassification("permanent", "failed")
    return ErrorClassification("internal", "failed")


def retry_at(
    attempt: int,
    *,
    now: datetime | None = None,
    base: float = 30,
    cap: float = 3600,
    jitter: float = 0.2,
) -> datetime:
    delay = min(cap, base * (2 ** max(0, attempt - 1))) * random.uniform(
        1 - jitter, 1 + jitter
    )
    return (now or datetime.now(UTC)) + timedelta(seconds=delay)
