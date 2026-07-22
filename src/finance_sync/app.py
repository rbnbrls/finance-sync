"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from finance_sync.api.middleware.rate_limit import RateLimitMiddleware
from finance_sync.api.v1.router import router as v1_router
from finance_sync.config.settings import Settings
from finance_sync.lifespan import lifespan
from finance_sync.observability.health import router as health_router
from finance_sync.observability.logging import (
    RequestLogMiddleware,
    configure_logging,
)
from finance_sync.observability.metrics import (
    PrometheusMiddleware,
    metrics_app,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return a fully configured FastAPI application.

    Parameters
    ----------
    settings:
        Optional override for testing.  When omitted, settings are loaded
        from environment variables / ``.env``.
    """
    if settings is None:
        settings = Settings()

    # ── Structured logging ──────────────────────────────────────────
    configure_logging(
        json_output=settings.is_production,
        log_level=settings.log_level,
    )

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.is_debug,
        lifespan=lifespan,
    )

    # Store settings on app.state so the lifespan handler can use them
    # (avoids re-reading from .env when custom settings were provided).
    app.state._settings = settings  # noqa: SLF001

    # ── Observability middleware stack ───────────────────────────────
    # Order: outermost first → Prometheus records first, then logging
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(PrometheusMiddleware)

    # ── CORS ─────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Rate limiting ────────────────────────────────────────────────
    app.add_middleware(RateLimitMiddleware)

    # ── API routers ──────────────────────────────────────────────────
    app.include_router(v1_router, prefix="/api/v1")

    # ── Health check endpoints (at root level for probes) ────────────
    app.include_router(health_router)

    # ── Prometheus metrics endpoint ──────────────────────────────────
    app.mount("/metrics", metrics_app)

    # ── Root redirect to docs ────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def root_redirect():
        """Redirect the root URL to the interactive API documentation."""
        return RedirectResponse(url="/docs")

    return app
