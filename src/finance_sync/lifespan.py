"""Application lifespan — initialise / tear down infrastructure."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

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

        async with container.engine.begin() as conn:
            # Enable pgcrypto extension (needed for gen_random_uuid())
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await conn.run_sync(Base.metadata.create_all)
            # Stamp alembic version so future migrations see a known baseline
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
            await conn.commit()

    async with container.dispose():
        yield  # app runs here
