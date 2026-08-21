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

from finance_sync.config.settings import secret_value
from finance_sync.dependencies import get_container
from finance_sync.services.github_issue import check_github_issue_access

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


async def _check_github(request: Request) -> dict[str, str]:
    """Verify the GitHub feedback-integration configuration.

    Checks that ``GITHUB_TOKEN`` is set and can actually access the
    configured ``GITHUB_REPO`` — a broken token (expired, revoked,
    missing scope) silently broke the feedback button for weeks before
    issue #273.  Returns ``not_configured`` without a network call when
    no token is set.
    """
    settings = get_container(request).settings
    github_token = secret_value(settings.github_token)
    if not github_token:
        return {
            "status": "not_configured",
            "detail": "GITHUB_TOKEN is not set",
        }
    return await check_github_issue_access(
        token=github_token,
        repo_full=settings.github_repo,
    )


@router.get("/health")
async def health_check(request: Request) -> dict[str, Any]:
    """Return overall health status with per-component checks."""
    components = {
        "database": await _check_database(request),
        "redis": await _check_redis(request),
        "github": await _check_github(request),
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
