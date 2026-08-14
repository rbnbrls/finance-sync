"""Alembic migration chain integration test.

Runs the **full** migration chain (``upgrade head``) against a dedicated,
freshly created PostgreSQL database — not the shared integration DB — so
the upgrade/downgrade round-trip cannot clobber other tests' data.

Asserts:
* ``alembic upgrade head`` on an empty database succeeds (linear chain)
* the expected application tables exist afterwards (incl. export tables)
* ``alembic downgrade base`` removes the schema entirely
* a re-``upgrade head`` re-creates it (round-trip)
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import run_alembic

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "tenants",
    "users",
    "api_keys",
    "credentials",
    "accounts",
    "securities",
    "security_listings",
    "transactions",
    "holdings",
    "balances",
    "outbox_messages",
    "sync_runs",
    "webhooks",
    "webhook_delivery_logs",
    "fx_rates",
    "fundamental_observations",
    "security_metadata_observations",
    "tax_lots",
    "reconciliation_runs",
    "reconciliation_results",
    "scheduled_payments",
    "card_transactions",
    "enrichment_freshness",
    "security_prices",
    "unresolved_securities",
    "detected_subscriptions",
    "resolution_audit_log",
    # exporter tables (migration 0008)
    "export_runs",
    "ab_account_mappings",
    "export_deliveries",
}


@pytest.fixture(scope="module")
async def fresh_database_url(database_url: str) -> AsyncGenerator[str, None]:
    """Create a dedicated database, yield its DSN, drop it afterwards."""
    url = make_url(database_url)
    db_name = f"finance_sync_migtest_{uuid.uuid4().hex[:8]}"

    # Connect to the maintenance DB (same server) to CREATE/DROP DATABASE.
    admin_url = url.set(database="postgres")
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await admin_engine.dispose()

    fresh_url = url.set(database=db_name)
    try:
        yield fresh_url.render_as_string(hide_password=False)
    finally:
        drop_engine = create_async_engine(
            admin_url.render_as_string(hide_password=False),
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with drop_engine.connect() as conn:
                await conn.execute(
                    sa.text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
                )
        finally:
            await drop_engine.dispose()


async def _public_tables(url: str) -> set[str]:
    """Return the set of public-schema table names in ``url``."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public'"
                )
            )
            return {row[0] for row in result}
    finally:
        await engine.dispose()


async def _alembic_version(url: str) -> str | None:
    """Return the current alembic_version, or None if absent."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            )
            row = result.first()
            return row[0] if row else None
    finally:
        await engine.dispose()


class TestMigrationUpgrade:
    async def test_upgrade_head_creates_full_schema(
        self, fresh_database_url: str
    ) -> None:
        """``alembic upgrade head`` on an empty DB creates every table."""
        run_alembic("upgrade", "head", url=fresh_database_url)

        tables = await _public_tables(fresh_database_url)
        assert "alembic_version" in tables
        missing = EXPECTED_TABLES - tables
        assert not missing, (
            f"missing tables after upgrade head: {sorted(missing)}"
        )

        version = await _alembic_version(fresh_database_url)
        assert version == "0010", f"expected head revision 0010, got {version}"

    async def test_single_head_linear_chain(
        self, fresh_database_url: str
    ) -> None:
        """``alembic history`` reports exactly one head (linear chain)."""
        run_alembic("upgrade", "head", url=fresh_database_url)

        env = {**os.environ, "ASYNC_DB_URL": fresh_database_url}
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "history"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        heads = [line for line in lines if "(head)" in line]
        assert len(heads) == 1, f"expected exactly one head, got: {heads}"

    async def test_downgrade_base_removes_schema(
        self, fresh_database_url: str
    ) -> None:
        """``alembic downgrade base`` removes every application table."""
        run_alembic("upgrade", "head", url=fresh_database_url)
        tables_after_upgrade = await _public_tables(fresh_database_url)
        assert "accounts" in tables_after_upgrade

        run_alembic("downgrade", "base", url=fresh_database_url)

        tables_after_downgrade = await _public_tables(fresh_database_url)
        # Alembic leaves the (empty) alembic_version table behind — but no
        # application tables may survive.
        remaining = tables_after_downgrade - {"alembic_version"}
        assert remaining == set(), (
            f"expected empty schema after downgrade base, got: "
            f"{sorted(remaining)}"
        )
        if "alembic_version" in tables_after_downgrade:
            engine = create_async_engine(fresh_database_url)
            try:
                async with engine.connect() as conn:
                    result = await conn.execute(
                        sa.text("SELECT COUNT(*) FROM alembic_version")
                    )
                    assert result.scalar() == 0
            finally:
                await engine.dispose()

    async def test_upgrade_after_downgrade_roundtrip(
        self, fresh_database_url: str
    ) -> None:
        """Re-upgrading after a downgrade re-creates the full schema."""
        run_alembic("upgrade", "head", url=fresh_database_url)
        run_alembic("downgrade", "base", url=fresh_database_url)
        run_alembic("upgrade", "head", url=fresh_database_url)

        tables = await _public_tables(fresh_database_url)
        missing = EXPECTED_TABLES - tables
        assert not missing, (
            f"missing tables after re-upgrade: {sorted(missing)}"
        )
        assert await _alembic_version(fresh_database_url) == "0010"

    async def test_upgrade_head_is_idempotent(
        self, fresh_database_url: str
    ) -> None:
        """Running upgrade head twice on a migrated DB is a no-op."""
        run_alembic("upgrade", "head", url=fresh_database_url)
        run_alembic("upgrade", "head", url=fresh_database_url)  # second run

        tables = await _public_tables(fresh_database_url)
        assert "accounts" in tables
        assert await _alembic_version(fresh_database_url) == "0010"
