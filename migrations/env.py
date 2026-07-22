"""Alembic environment configuration with async SQLAlchemy support.

This module configures Alembic to use the application's async engine
(created via the Container / Settings).  It requires an ``ASYNC_DB_URL``
environment variable (or a valid ``.env`` at the project root) pointing
at a PostgreSQL database that the migration user can DDL against.

Usage
-----
::

    # Run pending migrations against the target database
    ASYNC_DB_URL=postgresql+asyncpg://... alembic upgrade head

    # Autogenerate a new migration from model changes
    ASYNC_DB_URL=postgresql+asyncpg://... \\
        alembic revision --autogenerate -m "describe change"

    # Check for drift (no pending changes)
    ASYNC_DB_URL=postgresql+asyncpg://... alembic check
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

# ── Alembic Config object ───────────────────────────────────────────
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Target metadata ─────────────────────────────────────────────────
# Import all models so Alembic's autogenerate can detect them.

from finance_sync.db import Base  # noqa: E402
from finance_sync.models import *  # noqa: F403, E402

target_metadata = Base.metadata

# ── Other config values — read from app Settings ─────────────────────
# We use an env variable rather than importing the full Settings object
# to keep the migration env decoupled from the app at import time.
import os  # noqa: E402

ASYNC_DB_URL = os.environ.get(
    "ASYNC_DB_URL",
    os.environ.get("DATABASE_URL", ""),
)
if ASYNC_DB_URL:
    # Ensure it uses the asyncpg driver prefix
    if ASYNC_DB_URL.startswith("postgresql://"):
        ASYNC_DB_URL = ASYNC_DB_URL.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    elif ASYNC_DB_URL.startswith("postgresql+psycopg2://"):
        ASYNC_DB_URL = ASYNC_DB_URL.replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://", 1
        )
    config.set_main_option("sqlalchemy.url", ASYNC_DB_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' (--sql) mode.

    Context configures just the URL and emits SQL statements to stdout
    instead of executing them directly.  Useful for generating reviewable
    SQL or when the migration user cannot connect.
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        url = (
            ASYNC_DB_URL
            or "postgresql+asyncpg://user:pass@localhost/finance_sync"
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Execute migrations against a live connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a connection."""
    cfg = config.get_section(config.config_ini_section, {})
    if not cfg.get("sqlalchemy.url"):
        msg = (
            "No database URL configured.  Set the ASYNC_DB_URL or "
            "DATABASE_URL environment variable."
        )
        raise RuntimeError(msg)

    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
