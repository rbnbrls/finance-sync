"""Integration test harness — ephemeral real PostgreSQL + Redis.

This suite exercises the application against **real** PostgreSQL and Redis
instead of the aiosqlite mocks used by the fast unit suite.  It covers
repositories, the transactional outbox, the sync orchestrator, Redis-based
lock / rate-limit primitives, and the full Alembic migration chain.

The harness is environment-driven so the same tests run in CI (GitHub
Actions service containers) and locally (docker compose or an existing
PG/Redis on localhost):

* ``TEST_DATABASE_URL`` — asyncpg DSN (default
  ``postgresql+asyncpg://postgres:postgres@localhost:5432/finance_sync_test``)
* ``TEST_REDIS_URL`` — redis DSN (default ``redis://localhost:6379/15``)

When the env vars are **not** set the whole suite is skipped with a pointer
to the README, so a plain ``pytest`` run (unit suite) stays fast and green
on machines without Docker/PG/Redis.

Run locally:

    make test-integration            # docker compose + pytest -m integration

or manually:

    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/finance_sync_test \
    TEST_REDIS_URL=redis://localhost:6379/15 \
    uv run pytest -m integration -v
"""
# pyright: basic

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:5432/finance_sync_test"
)
DEFAULT_REDIS_URL = "redis://localhost:6379/15"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark every test in this directory as ``integration``.

    ``pytest_collection_modifyitems`` receives the **whole session's**
    items when pytest runs from the repo root (conftest hooks are not
    scoped to their directory), so only mark items whose nodeid lives
    under ``tests/integration/``.
    """
    for item in items:
        if item.nodeid.startswith("tests/integration"):
            item.add_marker(pytest.mark.integration)


# ── Connection URLs ──────────────────────────────────────────────────


@pytest.fixture(scope="session")
def database_url() -> str:
    """PostgreSQL DSN for the integration suite."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("TEST_DATABASE_URL must be set in CI")
    pytest.skip(
        "Integration tests need PostgreSQL + Redis. "
        "Run `make test-integration` (docker compose) or set "
        "TEST_DATABASE_URL / TEST_REDIS_URL — see README 'Integration tests'."
    )


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Redis DSN for the integration suite."""
    url = os.environ.get("TEST_REDIS_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("TEST_REDIS_URL must be set in CI")
    pytest.skip(
        "Integration tests need PostgreSQL + Redis. "
        "Run `make test-integration` (docker compose) or set "
        "TEST_DATABASE_URL / TEST_REDIS_URL — see README 'Integration tests'."
    )


# ── Alembic helpers ─────────────────────────────────────────────────


def run_alembic(*argv: str, url: str) -> None:
    """Run an alembic command against ``url`` in a subprocess.

    Uses ``sys.executable -m alembic`` so the migration env (and the
    ``finance_sync`` import inside it) resolves to the current venv —
    exactly the invocation the CI migrations job uses.
    """
    env = {**os.environ, "ASYNC_DB_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *argv],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic {' '.join(argv)} failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )


# ── Database fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_engine(database_url: str) -> AsyncEngine:
    """Session-scoped engine whose schema is migrated to ``head`` once.

    The fixture is intentionally *synchronous*: it only creates the engine
    object and runs ``alembic upgrade head`` via subprocess, so it does not
    depend on a pytest-asyncio event loop scope.
    """
    run_alembic("upgrade", "head", url=database_url)
    from finance_sync.db.json import default_json_serializer

    return create_async_engine(
        database_url,
        poolclass=NullPool,
        json_serializer=default_json_serializer,
        # NullPool avoids cross-event-loop connection reuse: pytest-asyncio
        # runs each test in its own loop, and a pooled asyncpg connection
        # bound to one loop cannot be used from another.
    )


@pytest.fixture(scope="session")
def session_factory(
    pg_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the migrated integration database."""
    return async_sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Fresh async session per test."""
    async with session_factory() as s:
        yield s


@pytest.fixture(autouse=True)
async def _truncate_tables(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    """Truncate all public tables (except ``alembic_version``) per test.

    Gives every test a clean database while keeping the migrated schema
    intact — no re-running migrations between tests.
    """
    yield
    async with pg_engine.begin() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: sa.inspect(sync_conn).get_table_names()
        )
        tables = [t for t in table_names if t != "alembic_version"]
        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(sa.text(f"TRUNCATE TABLE {quoted} CASCADE"))


# ── Redis fixtures ───────────────────────────────────────────────────


@pytest.fixture
async def redis_client(redis_url: str):
    """Function-scoped Redis client (DB flushed before each test)."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
