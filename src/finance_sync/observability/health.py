"""Health check endpoints for Kubernetes / Coolify probes.

Routes
------
- ``GET /health``        — component status summary
- ``GET /health/ready``  — readiness probe (DB / Redis reachable)
- ``GET /health/live``   — liveness probe (app is running)
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from finance_sync.dependencies import get_container

router = APIRouter(tags=["health"])

# Track application start time
_start_time: float = time.time()


def uptime() -> float:
    """Return application uptime in seconds."""
    return round(time.time() - _start_time, 2)


async def _check_database(request: Request) -> dict[str, str]:
    """Ping the configured database pool."""
    container = get_container(request)
    try:
        engine = container.engine
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except RuntimeError:
        return {"status": "not_configured"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def _check_redis(request: Request) -> dict[str, str]:
    """Ping the configured Redis instance."""
    container = get_container(request)
    try:
        r = container.redis_client
        await r.ping()  # type: ignore[union-attr]
        return {"status": "ok"}
    except RuntimeError:
        return {"status": "not_configured"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@router.get("/health")
async def health_check(request: Request) -> dict[str, Any]:
    """Return overall health status with per-component checks."""
    components = {
        "database": await _check_database(request),
        "redis": await _check_redis(request),
    }

    all_ok = all(
        c["status"] in ("ok", "not_configured") for c in components.values()
    )
    overall = "ok" if all_ok else "degraded"

    return {
        "status": overall,
        "version": get_container(request).settings.app_version,
        "uptime": uptime(),
        "components": components,
    }


@router.get("/health/ready")
async def readiness_check(request: Request) -> dict[str, Any]:
    """Readiness probe — confirms DB and Redis are reachable.

    Kubernetes / Coolify will only route traffic to this instance when
    this endpoint returns HTTP 200 with ``{"status": "ok"}``.
    """
    components = {
        "database": await _check_database(request),
        "redis": await _check_redis(request),
    }
    ready = all(
        c["status"] in ("ok", "not_configured") for c in components.values()
    )
    return {
        "status": "ok" if ready else "not_ready",
        "components": components,
    }


@router.get("/health/live")
async def liveness_check() -> dict[str, str]:
    """Liveness probe — the application process is alive.

    Returns ``{"status": "ok"}`` as long as the ASGI server is running.
    """
    return {"status": "ok"}
