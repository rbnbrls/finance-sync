"""Bounded rate limiting for opt-in remote destination probes."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status


class _SlidingWindowEntry:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps: list[float] = []

    def is_allowed(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self.timestamps = [
            timestamp for timestamp in self.timestamps if timestamp > cutoff
        ]
        if len(self.timestamps) >= self.max_requests:
            return False
        self.timestamps.append(now)
        return True


_fallback_limits: defaultdict[str, _SlidingWindowEntry] = defaultdict(
    lambda: _SlidingWindowEntry(10, 60)
)


def _probe_key(request: Request) -> str:
    credential = request.headers.get("x-api-key") or request.headers.get(
        "authorization", ""
    )
    digest = hashlib.sha256(credential.encode()).hexdigest()[:32]
    target_id = str(request.path_params.get("target_id") or "unknown")
    return f"destination-probe:{target_id}:{digest}"


async def _redis_allowed(
    request: Request, key: str, max_requests: int, window_seconds: int
) -> bool | None:
    container = getattr(request.app.state, "container", None)
    if container is None:
        return None
    try:
        redis = container.redis_client
        bucket = int(time.time() // window_seconds)
        redis_key = f"finance-sync:{key}:{bucket}"
        count = int(await redis.incr(redis_key))
        if count == 1:
            await redis.expire(redis_key, window_seconds + 1)
        return count <= max_requests
    except Exception:
        return None


async def check_destination_probe_rate_limit(request: Request) -> None:
    """Limit remote probes per credential and target, with Redis sharing."""
    settings = request.app.state.container.settings
    if not settings.destination_remote_probe_enabled:
        return
    max_requests = settings.destination_probe_rate_limit_max_requests
    window_seconds = settings.destination_probe_rate_limit_window_seconds
    key = _probe_key(request)
    allowed = await _redis_allowed(request, key, max_requests, window_seconds)
    if allowed is None:
        entry = _fallback_limits[key]
        entry.max_requests = max_requests
        entry.window_seconds = window_seconds
        allowed = entry.is_allowed()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "Destination probe rate limit exceeded",
                "retry_after_seconds": window_seconds,
                "limit": max_requests,
                "window_seconds": window_seconds,
            },
            headers={"Retry-After": str(window_seconds)},
        )
