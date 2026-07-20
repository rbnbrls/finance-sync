"""AI-specific rate limiting dependency.

Provides a per-client sliding-window rate limiter for the AI summary
endpoints, separate from the global API rate limit.  This prevents
excessive LLM API calls that would drive up costs.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status


class _AISlidingWindowEntry:
    """Tracks request timestamps within a sliding window."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps: list[float] = []

    def is_allowed(self) -> bool:
        """Check and record a request. Returns True if under the limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]

        if len(self.timestamps) >= self.max_requests:
            return False

        self.timestamps.append(now)
        return True


# Per-prefix client tracking
_ai_limits: defaultdict[str, _AISlidingWindowEntry] = defaultdict(
    lambda: _AISlidingWindowEntry(20, 3600)  # defaults: 20/hr
)


def check_ai_rate_limit(request: Request) -> None:
    """FastAPI dependency: enforce AI-specific rate limits.

    Uses settings values from the request's app state for
    max_requests and window_seconds so the limits are configurable.

    Usage::

        @router.post("/summary")
        async def my_endpoint(
            _: None = Depends(check_ai_rate_limit),
        ):
            ...
    """
    settings = request.app.state.container.settings  # type: ignore[union-attr]

    max_r = settings.ai_rate_limit_max_requests
    window_s = settings.ai_rate_limit_window_seconds

    # Derive client key (same logic as the global middleware)
    api_key = request.headers.get("x-api-key") or request.headers.get(
        "authorization", ""
    )
    if api_key:
        key = f"key:{api_key[:32]}"
    else:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            key = f"ip:{forwarded.split(',')[0].strip()}"
        else:
            client = request.client
            key = f"ip:{client.host}" if client is not None else "ip:unknown"

    entry = _ai_limits[key]
    # Update entry config in case settings changed
    entry.max_requests = max_r
    entry.window_seconds = window_s

    if not entry.is_allowed():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "AI rate limit exceeded",
                "retry_after_seconds": int(window_s),
                "limit": max_r,
                "window_seconds": window_s,
            },
        )
