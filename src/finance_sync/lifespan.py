"""Application lifespan — initialise / tear down infrastructure."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text

# Import all models so they register on Base.metadata for create_all
from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.db import Base
from finance_sync.models import ensure_exporter_models_loaded

ALEMBIC_HEAD: str = "0003"

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI


logger = structlog.get_logger("finance_sync.lifespan")

_DB_RETRIES: int = 5
_DB_RETRY_DELAY_S: float = 2.0
_DB_RETRY_BACKOFF: float = 2.0


async def _init_database(container: Container) -> None:
    """Connect to the database and apply schema / seed data.

    Retries with exponential backoff on transient failures so that a
    momentarily-unavailable database does not crash the whole app container.
    """
    last_exc: Exception | None = None

    for attempt in range(1, _DB_RETRIES + 1):
        try:
            async with container.engine.begin() as conn:
                # Enable pgcrypto extension (needed for gen_random_uuid())
                await conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS pgcrypto")
                )
                await conn.run_sync(Base.metadata.create_all)
                # Stamp alembic version so future migrations see a known
                # baseline
                await conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS alembic_version "
                        "(version_num VARCHAR(32) PRIMARY KEY)"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO alembic_version (version_num) "
                        "VALUES (:head) "
                        "ON CONFLICT (version_num) DO NOTHING"
                    ),
                    {"head": ALEMBIC_HEAD},
                )
                # ── Seed default tenant and admin user (idempotent) ────
                from datetime import UTC, datetime

                from finance_sync.services.auth import hash_password

                now = datetime.now(UTC)

                # Create default tenant if it doesn't exist
                tenant_row = await conn.execute(
                    text("SELECT id FROM tenants WHERE slug = 'default'")
                )
                tenant = tenant_row.first()
                if tenant is None:
                    tenant_id = await conn.execute(
                        text(
                            "INSERT INTO tenants "
                            "(id, slug, name, created_at, updated_at) "
                            "VALUES (gen_random_uuid(), 'default', "
                            "'Default Tenant', :now, :now) RETURNING id"
                        ),
                        {"now": now},
                    )
                    tid = tenant_id.scalar_one()
                    logger.info("created_default_tenant")
                else:
                    tid = tenant[0]

                # Create admin user if it doesn't exist
                user_row = await conn.execute(
                    text(
                        "SELECT id FROM users "
                        "WHERE email = 'admin@finance-sync.local'"
                    )
                )
                if user_row.first() is None:
                    pwd = hash_password("admin")
                    await conn.execute(
                        text(
                            "INSERT INTO users "
                            "(id, tenant_id, email, hashed_password, "
                            "display_name, role, is_active, "
                            "created_at, updated_at) "
                            "VALUES (gen_random_uuid(), :tid, "
                            " 'admin@finance-sync.local', :pwd, "
                            "'Admin', 'admin', true, :now, :now)"
                        ),
                        {"tid": tid, "pwd": pwd, "now": now},
                    )
                    logger.info(
                        "seeded_admin_user",
                        email="admin@finance-sync.local",
                    )
                else:
                    logger.info(
                        "admin_user_exists",
                        email="admin@finance-sync.local",
                    )
                await conn.commit()

            # Success — exit the retry loop
            logger.info("database_initialised", attempt=attempt)
            return

        except Exception as exc:
            last_exc = exc
            if attempt < _DB_RETRIES:
                delay = _DB_RETRY_DELAY_S * (_DB_RETRY_BACKOFF ** (attempt - 1))
                logger.warning(
                    "database_init_attempt_failed",
                    attempt=attempt,
                    max_retries=_DB_RETRIES,
                    retry_delay_s=round(delay, 1),
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "database_init_failed",
                    attempts=attempt,
                    error=str(exc),
                )

    # All retries exhausted — re-raise so the app crashes with a clear
    # message rather than silently serving with no schema.
    if last_exc is not None:
        raise last_exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """FastAPI lifespan context manager.

    Startup
    -------
    * Use the settings already stored on ``app.state`` (set by
      ``create_app``) or load from environment / ``.env``.
    * Build the DI container (DB engine, Redis pool).
    * Store the container on ``app.state`` so route handlers can access
      it via ``request.app.state.container``.
    * Auto-create all database tables defined by SQLAlchemy models
      (``Base.metadata.create_all``).  This is a safety net — in
      production, migrations are managed via Alembic.

    Shutdown
    --------
    * Dispose the DB engine.
    * Close the Redis connection.
    """
    # If create_app already stored settings on state, use them
    settings: Settings = getattr(app.state, "_settings", None) or Settings()
    container = Container.from_settings(settings)

    # Store so route handlers can access via request.app.state.container
    app.state.container = container

    # -- Auto-create database tables -----------------------------------
    if settings.database_url is not None:
        # Ensure lazy-loaded exporter models are registered on metadata
        ensure_exporter_models_loaded()
        await _init_database(container)

    async with container.dispose():
        yield  # app runs here
