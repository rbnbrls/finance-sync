"""E2E test harness — full app + worker + real PostgreSQL + Redis.

This suite (Gap G-10, roadmap tc.4 / ms.2.ac.1) proves the end-to-end
**API → transactional outbox → background worker** flow produces an
*exactly-once observable outcome* under at-least-once delivery.

It reuses the ephemeral PG/Redis harness introduced by G-09
(``tests/integration/conftest.py``): the ``database_url`` / ``redis_url``
fixtures skip the whole suite when ``TEST_DATABASE_URL`` /
``TEST_REDIS_URL`` are unset, and run ``alembic upgrade head`` once per
session.  The E2E fixtures on top build a real FastAPI app wired to that
database and a real worker (``process_outbox_job``) driving webhook
deliveries against a local capture server.

Run locally:

    make test-e2e                          # docker compose + pytest -m e2e

or manually:

    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/finance_sync_test \
    TEST_REDIS_URL=redis://localhost:6379/15 \
    uv run pytest -m e2e -v

Like the integration suite, a plain ``pytest`` run (unit job) stays fast:
the e2e suite is deselected with ``-m "not integration and not e2e"`` and
skips entirely when the env vars are missing.
"""

# pyright: basic
# The harness imports pytest/sqlalchemy fixture plumbing that pyright
# cannot fully resolve from a bare interpreter; the integration conftest
# uses the same relaxation.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.container import Container

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

# ── Reuse the G-09 integration harness ──────────────────────────────
# The fixture functions below are plain functions (pytest does not wrap
# them), so importing and re-registering them here exposes the same
# session-scoped PG/Redis setup — including the skip-when-unset and
# fail-in-CI semantics — to the tests/e2e/ tree.
from tests.integration.conftest import (
    database_url as _integration_database_url,
)
from tests.integration.conftest import (
    pg_engine as _integration_pg_engine,
)
from tests.integration.conftest import (
    redis_url as _integration_redis_url,
)
from tests.integration.conftest import (
    session_factory as _integration_session_factory,
)

database_url = _integration_database_url
pg_engine = _integration_pg_engine
redis_url = _integration_redis_url
session_factory = _integration_session_factory

# 32-byte AES-256-GCM master key (hex) for credential envelope encryption.
_E2E_MASTER_KEY = "0123456789abcdef" * 4
_E2E_SECRET = "e2e-test-secret-key-32chars!!"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark every test under ``tests/e2e/`` as ``e2e``.

    Same pattern as the integration conftest: the hook receives the whole
    session's items, so only mark nodeids under this directory.
    """
    for item in items:
        if item.nodeid.startswith("tests/e2e"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(autouse=True)
async def _truncate_tables(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    """Truncate all public tables (except ``alembic_version``) per test.

    Runs **before and after** each test so a test is hermetic even when a
    previous run crashed hard (kill -9, interpreter abort) before its own
    teardown could clean up.  Mirrors the integration harness semantics
    while keeping the migrated schema intact.
    """

    async def _truncate() -> None:
        async with pg_engine.begin() as conn:
            table_names = await conn.run_sync(
                lambda sync_conn: sa.inspect(sync_conn).get_table_names()
            )
            tables = [t for t in table_names if t != "alembic_version"]
            if tables:
                quoted = ", ".join(f'"{t}"' for t in tables)
                await conn.execute(sa.text(f"TRUNCATE TABLE {quoted} CASCADE"))

    await _truncate()
    try:
        yield
    finally:
        await _truncate()


# ── E2E application stack ────────────────────────────────────────────


@pytest.fixture(scope="session")
def e2e_settings(database_url: str, redis_url: str) -> Settings:
    """Settings pointing at the ephemeral PG/Redis, worker recon off."""
    return Settings(
        database_url=database_url,  # pyright: ignore[reportArgumentType]
        redis_url=redis_url,  # pyright: ignore[reportArgumentType]
        secret_key=_E2E_SECRET,  # pyright: ignore[reportArgumentType]
        master_encryption_key=_E2E_MASTER_KEY,  # pyright: ignore[reportArgumentType]
        # Keep the sync pipeline focused: no auto-reconciliation noise
        # on top of the ingestion + outbox assertions.
        worker_job_reconciliation_after_sync_enabled=False,
    )


@pytest.fixture
def e2e_container(
    e2e_settings: Settings,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> Container:
    """DI container for the app + worker, bound to the harness DB.

    The harness engine uses ``NullPool`` so connections are never reused
    across pytest-asyncio event loops; the app and the worker share this
    container exactly like a production deployment shares one container.
    """
    container = Container.from_settings(e2e_settings)
    # Override the pooled engine with the harness's NullPool engine —
    # same settings, loop-safe connection handling.
    container._engine = pg_engine  # pyright: ignore[reportPrivateUsage]
    container._session_factory = session_factory  # pyright: ignore[reportPrivateUsage]
    return container


@pytest.fixture
def e2e_app(
    e2e_container: Container,
    e2e_settings: Settings,
) -> FastAPI:
    """FastAPI app with the container attached (lifespan not run —
    ``ASGITransport`` does not trigger it; the harness seeds state
    directly, and ``_init_database``'s default-tenant seeding is
    intentionally bypassed for a clean per-test tenant).
    """
    app = create_app(settings=e2e_settings)
    app.state.container = e2e_container
    return app
