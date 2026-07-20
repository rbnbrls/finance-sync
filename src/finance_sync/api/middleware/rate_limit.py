"""In-memory rate-limiting middleware for FastAPI.

Uses a simple sliding-window counter per client (IP address or API key)
to enforce a maximum number of requests per window.  When the limit is
exceeded the middleware returns ``429 Too Many Requests``.

For distributed deployments, replace with a Redis-backed limiter.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request, Response
    from starlette.types import ASGIApp


class SlidingWindowEntry:
    """Tracks request timestamps within a sliding window."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps: list[float] = []

    def is_allowed(self) -> bool:
        """Check and record a request.  Returns True if under the limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        # Prune old entries
        self.timestamps = [t for t in self.timestamps if t > cutoff]

        if len(self.timestamps) >= self.max_requests:
            return False

        self.timestamps.append(now)
        return True

    def reset(self) -> None:
        """Clear all tracked timestamps."""
        self.timestamps.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter based on client identity.

    Configurable defaults (per-route overrides) through the
    ``rate_limit_config`` attribute on the app state.

    Usage in ``create_app``::

        from finance_sync.api.middleware.rate_limit import RateLimitMiddleware
        app.add_middleware(RateLimitMiddleware)
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_max_requests: int = 200,
        default_window_seconds: float = 60.0,
        exempt_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._default_max = default_max_requests
        self._default_window = default_window_seconds
        self._exempt_paths = exempt_paths or {
            "/metrics",
            "/health",
            "/openapi.json",
            "/docs",
            "/redoc",
        }
        self._clients: dict[str, SlidingWindowEntry] = defaultdict(
            lambda: SlidingWindowEntry(
                default_max_requests, default_window_seconds
            )
        )

    def _client_key(self, request: Request) -> str:
        """Derive a client identity from the request."""
        # Prefer API key header, fall back to IP
        api_key = request.headers.get("x-api-key") or request.headers.get(
            "authorization", ""
        )
        if api_key:
            # Truncate long tokens to avoid memory bloat
            return f"key:{api_key[:32]}"
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
        client = request.client
        if client is not None:
            return f"ip:{client.host}"
        return "ip:unknown"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Skip exempt paths
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        key = self._client_key(request)
        entry = self._clients[key]

        if not entry.is_allowed():
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests",
                    "retry_after_seconds": int(entry.window_seconds),
                },
                headers={
                    "Retry-After": str(int(entry.window_seconds)),
                    "X-RateLimit-Limit": str(entry.max_requests),
                },
            )

        response = await call_next(request)

        # Attach rate-limit headers
        remaining = max(0, entry.max_requests - len(entry.timestamps))
        response.headers["X-RateLimit-Limit"] = str(entry.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(
            int(time.time() + entry.window_seconds)
        )

        return response
