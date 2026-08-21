"""In-memory rate-limiting middleware for FastAPI.

Uses a simple sliding-window counter per client (IP address or API key)
to enforce a maximum number of requests per window.  When the limit is
exceeded the middleware returns ``429 Too Many Requests``.

For distributed deployments, replace with a Redis-backed limiter.
"""

from __future__ import annotations

import hashlib
import ipaddress
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

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
            digest = hashlib.sha256(api_key.encode()).hexdigest()[:32]
            return f"key:{digest}"
        client = request.client
        client_host = client.host if client is not None else None
        settings = None
        container = getattr(request.app.state, "container", None)
        if container is not None:
            try:
                settings = container.settings
            except RuntimeError:
                settings = None
        trusted_proxies = getattr(settings, "trusted_proxy_ips", [])
        if client_host and self._is_trusted_proxy(client_host, trusted_proxies):
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return f"ip:{forwarded.split(',')[0].strip()}"
        if client is not None:
            return f"ip:{client.host}"
        return "ip:unknown"

    @staticmethod
    def _is_trusted_proxy(host: str, trusted: list[str]) -> bool:
        """Return whether *host* matches a configured proxy IP or CIDR."""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        for item in trusted:
            try:
                if address in ipaddress.ip_network(item, strict=False):
                    return True
            except ValueError:
                continue
        return False

    async def _redis_limit(
        self, request: Request, key: str
    ) -> tuple[bool, int, int] | None:
        """Apply a shared Redis fixed-window limit when Redis is available."""
        container = getattr(request.app.state, "container", None)
        if container is None:
            return None
        try:
            redis: Any = container.redis_client
            bucket = int(time.time() // self._default_window)
            redis_key = f"finance-sync:ratelimit:{key}:{bucket}"
            count = int(await redis.incr(redis_key))
            if count == 1:
                await redis.expire(redis_key, int(self._default_window) + 1)
            ttl = int(await redis.ttl(redis_key))
            return count <= self._default_max, count, max(ttl, 1)
        except Exception:
            # Local fallback keeps development usable; production health and
            # deployment checks must ensure Redis is available.
            return None

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Skip exempt paths
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        key = self._client_key(request)
        redis_result = await self._redis_limit(request, key)
        if redis_result is not None:
            allowed, count, reset_seconds = redis_result
            remaining = max(0, self._default_max - count)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Too many requests",
                        "retry_after_seconds": reset_seconds,
                    },
                    headers={
                        "Retry-After": str(reset_seconds),
                        "X-RateLimit-Limit": str(self._default_max),
                        "X-RateLimit-Remaining": "0",
                    },
                )
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = str(self._default_max)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = str(
                int(time.time() + reset_seconds)
            )
            return response

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
