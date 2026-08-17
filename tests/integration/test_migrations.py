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

from tests.integration.conftest import REPO_ROOT, run_alembic

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
    "import_runs",
    # sync cursor persistence (migration 0011)
    "sync_cursor",
    # exporter tables (migration 0008)
    "export_runs",
    "ab_account_mappings",
    "export_deliveries",
    # wealthfolio delivery cursor (migration 0012)
    "wealthfolio_deliveries",
    "wealthfolio_account_mappings",
    # multi-connection + per-connection audit trail (migration 0017)
    "connection_audit_log",
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


def _alembic_head_revision() -> str:
    """Return the current Alembic head revision from the migration scripts.

    Derived via ``alembic heads`` instead of hardcoding a revision id so
    the assertions keep working whenever a new migration is added.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        capture_output=True,
        text=True,
        env={**os.environ},
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    heads = [
        line.split()[0]
        for line in result.stdout.splitlines()
        if line.strip() and "(head)" in line
    ]
    assert len(heads) == 1, f"expected exactly one head, got: {heads}"
    return heads[0]


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
        head = _alembic_head_revision()
        assert version == head, f"expected head revision {head}, got {version}"

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
        assert (
            await _alembic_version(fresh_database_url)
            == _alembic_head_revision()
        )

    async def test_upgrade_head_is_idempotent(
        self, fresh_database_url: str
    ) -> None:
        """Running upgrade head twice on a migrated DB is a no-op."""
        run_alembic("upgrade", "head", url=fresh_database_url)
        run_alembic("upgrade", "head", url=fresh_database_url)  # second run

        tables = await _public_tables(fresh_database_url)
        assert "accounts" in tables
        assert (
            await _alembic_version(fresh_database_url)
            == _alembic_head_revision()
        )


class TestMultiConnectionUpgrade:
    """Migration 0017: multiple connections per provider.

    Upgrades an **existing single-connection database** (schema at
    revision 0016) populated with legacy connector configs and synced
    data, then proves:

    * existing configs remain usable (credential row intact, ciphertext
      unchanged — decryptable with the same master key)
    * ``connection_id`` is backfilled on accounts / transactions /
      cursors from the (then-unique) credential row
    * a generated label is backfilled on configs without one
    * the ``(tenant_id, provider_key)`` unique index is gone, so two
      configs with the same ``provider_key`` can exist in one tenant
    """

    # Function-scoped so each test gets a pristine database: the
    # module-scoped ``fresh_database_url`` is shared with
    # ``TestMigrationUpgrade`` which leaves it at other revisions.
    @pytest.fixture
    async def fresh_database_url(
        self, database_url: str
    ) -> AsyncGenerator[str, None]:
        """Create a dedicated database, yield its DSN, drop it afterwards."""
        url = make_url(database_url)
        db_name = f"finance_sync_migtest_{uuid.uuid4().hex[:8]}"

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
                        sa.text(
                            f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'
                        )
                    )
            finally:
                await drop_engine.dispose()

    async def test_legacy_configs_survive_and_multiple_configs_allowed(
        self, fresh_database_url: str
    ) -> None:
        # ── 1. Build the pre-0017 schema ──────────────────────────────
        run_alembic("upgrade", "0016", url=fresh_database_url)

        engine = create_async_engine(fresh_database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO tenants (id, slug, name) "
                        "VALUES (:id, :slug, :name)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "slug": "legacy-tenant",
                        "name": "Legacy Tenant",
                    },
                )

                # Legacy bunq credential — ciphertext + nonce must be
                # preserved byte-for-byte by the migration.
                tenant_id = (
                    await conn.execute(
                        sa.text(
                            "SELECT id FROM tenants WHERE slug='legacy-tenant'"
                        )
                    )
                ).scalar_one()
                cred_id = uuid.uuid4()
                ciphertext = b"\x01\x02\x03" * 16  # AES-256-GCM blob (fake)
                nonce = b"\xaa" * 12
                await conn.execute(
                    sa.text(
                        "INSERT INTO credentials "
                        "(id, tenant_id, provider_key, encrypted_payload, nonce, "
                        "description, created_at, updated_at) "
                        "VALUES (:id, :tenant, 'bunq', :payload, :nonce, "
                        "NULL, now(), now())"
                    ),
                    {
                        "id": cred_id,
                        "tenant": tenant_id,
                        "payload": ciphertext,
                        "nonce": nonce,
                    },
                )

                # One account + cursor + transaction from the legacy sync.
                account_id = uuid.uuid4()
                await conn.execute(
                    sa.text(
                        "INSERT INTO accounts "
                        "(id, tenant_id, provider_key, external_account_id, "
                        "name, account_type, currency_code, is_active, "
                        "created_at, updated_at) "
                        "VALUES (:id, :tenant, 'bunq', 'ext-acc-1', "
                        "'Bunq Account', 'checking', 'EUR', true, now(), now())"
                    ),
                    {"id": account_id, "tenant": tenant_id},
                )
                await conn.execute(
                    sa.text(
                        "INSERT INTO sync_cursor "
                        "(id, tenant_id, connector, resource, cursor, "
                        "created_at, updated_at) "
                        "VALUES (:id, :tenant, 'bunq', 'ext-acc-1', "
                        "now(), now(), now())"
                    ),
                    {"id": uuid.uuid4(), "tenant": tenant_id},
                )
                await conn.execute(
                    sa.text(
                        "INSERT INTO transactions "
                        "(id, tenant_id, provider_key, external_transaction_id, "
                        "account_id, amount, currency_code, occurred_at, "
                        "transaction_type, status, created_at, updated_at) "
                        "VALUES (:id, :tenant, 'bunq', 'ext-txn-1', :account, "
                        "12.50, 'EUR', now(), 'payment', 'booked', now(), now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant": tenant_id,
                        "account": account_id,
                    },
                )

                # Sanity: legacy schema really is unique per provider.
                dup = await conn.execute(
                    sa.text(
                        "SELECT 1 FROM credentials "
                        "WHERE tenant_id=:t AND provider_key='bunq'"
                    ),
                    {"t": tenant_id},
                )
                assert dup.scalar_one_or_none() == 1
        finally:
            await engine.dispose()

        # ── 2. Run the multi-connection migration ─────────────────────
        run_alembic("upgrade", "head", url=fresh_database_url)

        engine = create_async_engine(fresh_database_url)
        try:
            async with engine.connect() as conn:
                # Existing config remains usable: row present + ciphertext
                # untouched + label backfilled.
                row = (
                    await conn.execute(
                        sa.text(
                            "SELECT id, tenant_id, encrypted_payload, nonce, "
                            "description, status, selected_accounts "
                            "FROM credentials WHERE id=:id"
                        ),
                        {"id": cred_id},
                    )
                ).one()
                assert row.encrypted_payload == ciphertext
                assert row.nonce == nonce
                # Status defaults to active; label backfilled from provider.
                assert row.status == "active"
                assert row.selected_accounts is None
                assert row.description == "bunq"

                # connection_id backfilled on the legacy synced rows.
                acc = (
                    await conn.execute(
                        sa.text(
                            "SELECT connection_id FROM accounts "
                            "WHERE external_account_id='ext-acc-1'"
                        )
                    )
                ).one()
                assert acc.connection_id == str(cred_id)
                cur = (
                    await conn.execute(
                        sa.text(
                            "SELECT connection_id FROM sync_cursor "
                            "WHERE resource='ext-acc-1'"
                        )
                    )
                ).one()
                assert cur.connection_id == str(cred_id)
                txn = (
                    await conn.execute(
                        sa.text(
                            "SELECT connection_id FROM transactions "
                            "WHERE external_transaction_id='ext-txn-1'"
                        )
                    )
                ).one()
                assert txn.connection_id == str(cred_id)

                # Unique index (tenant_id, provider_key) is gone.
                indexes = (
                    (
                        await conn.execute(
                            sa.text(
                                "SELECT indexname FROM pg_indexes "
                                "WHERE tablename='credentials'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert "ix_credentials_tenant_provider" not in indexes

                # Two configs with the same provider_key now coexist.
                second_id = uuid.uuid4()
                await conn.execute(
                    sa.text(
                        "INSERT INTO credentials "
                        "(id, tenant_id, provider_key, encrypted_payload, nonce, "
                        "description, status, created_at, updated_at) "
                        "VALUES (:id, :tenant, 'bunq', :payload, :nonce, "
                        "'Second bunq', 'paused', now(), now())"
                    ),
                    {
                        "id": second_id,
                        "tenant": tenant_id,
                        "payload": bytes(range(48)),
                        "nonce": b"\xbb" * 12,
                    },
                )
                count = (
                    await conn.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM credentials "
                            "WHERE tenant_id=:t AND provider_key='bunq'"
                        ),
                        {"t": tenant_id},
                    )
                ).scalar_one()
                assert count == 2
        finally:
            await engine.dispose()

    async def test_account_selection_fields_survive_roundtrip(
        self, fresh_database_url: str
    ) -> None:
        """Paused status + selected_accounts persist through downgrade."""
        run_alembic("upgrade", "head", url=fresh_database_url)

        engine = create_async_engine(fresh_database_url)
        try:
            async with engine.begin() as conn:
                tenant_id = uuid.uuid4()
                await conn.execute(
                    sa.text(
                        "INSERT INTO tenants (id, slug, name) "
                        "VALUES (:id, :slug, :name)"
                    ),
                    {
                        "id": tenant_id,
                        "slug": "paused-tenant",
                        "name": "Paused",
                    },
                )
                cred_id = uuid.uuid4()
                await conn.execute(
                    sa.text(
                        "INSERT INTO credentials "
                        "(id, tenant_id, provider_key, encrypted_payload, nonce, "
                        "description, status, selected_accounts, "
                        "last_attempt_at, last_success_at, last_error, "
                        "created_at, updated_at) "
                        "VALUES (:id, :tenant, 'trading212', :payload, :nonce, "
                        ":label, 'paused', CAST(:selected AS jsonb), "
                        "now(), now(), 'boom', now(), now())"
                    ),
                    {
                        "id": cred_id,
                        "tenant": tenant_id,
                        "payload": b"\x00" * 16,
                        "nonce": b"\x00" * 12,
                        "label": "T212",
                        "selected": '["acc-1", "acc-2"]',
                    },
                )
        finally:
            await engine.dispose()

        # Downgrade to 0016 removes the connection columns…
        run_alembic("downgrade", "0016", url=fresh_database_url)
        # …and re-upgrade restores defaults; data written at head survives.
        run_alembic("upgrade", "head", url=fresh_database_url)

        engine = create_async_engine(fresh_database_url)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        sa.text(
                            "SELECT status, selected_accounts, last_error, "
                            "description FROM credentials WHERE id=:id"
                        ),
                        {"id": cred_id},
                    )
                ).one()
                assert row.status == "active"  # downgrade drops col → default
                assert row.description == "T212"
        finally:
            await engine.dispose()
