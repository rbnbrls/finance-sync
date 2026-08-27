"""PG regression: sync-runs stats query joins uuid to uuid (issue #451).

Reproduces the exact failure from GlitchTip #4 / issue #451 — the
sync-runs stats query in ``ReadService.list_sync_runs`` joins
``credentials.id`` (uuid) to ``sync_runs.connection_id``.  Before
migration 0045 the connection_id column was ``varchar(64)``, so that
join compiled to ``uuid = character varying`` and PostgreSQL raised

    UndefinedFunctionError: operator does not exist: uuid = character varying

After migration 0045 both sides are native ``uuid`` and the bare join
compiles to ``uuid = uuid`` and executes.

This test is the *real-data* regression guard that the SQLite unit suite
cannot provide: it runs against a migrated PostgreSQL database, creates a
credential and several sync runs, and asserts the connector/status counts
come back from the actual stats query.  It fails on the pre-0045 schema
(uuid = varchar) and passes after the fix.  No casts appear anywhere in
the test — the join must work with native types alone.

See also ``tests/test_read_api.py::test_tenant_join_uses_matching_uuid_types``
(the compile-time no-CAST guard) and ``tests/integration/test_migrations.py``
(the schema-level guard).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from finance_sync.models import Credential, SyncRun, Tenant
from finance_sync.models.enums import SyncRunStatus
from finance_sync.services.read_api import ReadService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


async def _seed_tenant_credential_runs(
    session: AsyncSession,
    *,
    tenant_slug: str,
    connector: str = "bunq",
) -> tuple[Tenant, Credential]:
    """Create one tenant, one credential and four sync runs in it.

    Returns the tenant and credential so the caller can scope the stats
    query the way the API does (``GET /api/v1/sync-runs`` passes
    ``auth.tenant_id``).
    """
    tenant = Tenant(slug=tenant_slug, name=tenant_slug)
    session.add(tenant)
    await session.flush()

    cred = Credential(
        tenant_id=tenant.id,
        provider_key=connector,
        encrypted_payload=b"\x00" * 16,
        nonce=b"\x00" * 12,
        status="active",
    )
    session.add(cred)
    await session.flush()

    now = datetime.now(UTC)
    runs = [
        SyncRun(
            connector=connector,
            connection_id=cred.id,
            status=SyncRunStatus.COMPLETED,
            started_at=now - timedelta(hours=3),
            completed_at=now - timedelta(hours=2, minutes=58),
            items_processed=42,
        ),
        SyncRun(
            connector=connector,
            connection_id=cred.id,
            status=SyncRunStatus.COMPLETED,
            started_at=now - timedelta(hours=2),
            completed_at=now - timedelta(hours=1, minutes=58),
            items_processed=17,
        ),
        SyncRun(
            connector=connector,
            connection_id=cred.id,
            status=SyncRunStatus.FAILED,
            started_at=now - timedelta(hours=1),
            completed_at=now - timedelta(minutes=58),
            error_category="authentication",
            error_message="credentials rejected",
        ),
        SyncRun(
            connector="plaid",
            connection_id=cred.id,
            status=SyncRunStatus.RUNNING,
            started_at=now - timedelta(minutes=5),
        ),
    ]
    session.add_all(runs)
    await session.commit()
    return tenant, cred


async def test_sync_runs_stats_join_uuid_uuid(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stats query executes on PG and returns connector/status counts.

    Regression for issue #451: on the pre-0045 schema this raises
    ``UndefinedFunctionError: operator does not exist: uuid = character
    varying`` from the tenant-scoped join in ``list_sync_runs``; with the
    0045 uuid = uuid schema the counts come back correctly.
    """
    tenant, _cred = await _seed_tenant_credential_runs(
        session, tenant_slug="sync-runs-stats"
    )

    async with session_factory() as s:
        svc = ReadService(s)
        result = await svc.list_sync_runs(tenant_id=str(tenant.id))

    assert result.total == 4
    assert len(result.items) == 4

    # Connector/status counts from the exact stats query
    # (SELECT connector, status, count(*) ... GROUP BY connector, status).
    counts = {(c.connector, c.status): c.count for c in result.status_counts}
    assert counts[("bunq", "completed")] == 2
    assert counts[("bunq", "failed")] == 1
    assert counts[("plaid", "running")] == 1
    # Nothing outside the tenant's credentials is counted.
    assert sum(counts.values()) == 4


async def test_sync_runs_stats_join_isolates_other_tenant(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The tenant-scoped join does not leak runs from other tenants."""
    tenant, _cred = await _seed_tenant_credential_runs(
        session, tenant_slug="sync-runs-stats-iso"
    )

    # A second tenant with its own credential + run — must not show up.
    other = Tenant(slug="sync-runs-stats-other", name="other")
    session.add(other)
    await session.flush()
    other_cred = Credential(
        tenant_id=other.id,
        provider_key="teller",
        encrypted_payload=b"\x00" * 16,
        nonce=b"\x00" * 12,
        status="active",
    )
    session.add(other_cred)
    await session.flush()
    session.add(
        SyncRun(
            connector="teller",
            connection_id=other_cred.id,
            status=SyncRunStatus.FAILED,
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await session.commit()

    async with session_factory() as s:
        svc = ReadService(s)
        result = await svc.list_sync_runs(tenant_id=str(tenant.id))

    assert result.total == 4
    assert all(r.connector != "teller" for r in result.items)
    assert all(c.connector != "teller" for c in result.status_counts)


async def test_sync_runs_stats_join_with_uuid_formatted_string_ids(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """connection_id as a UUID-formatted *string* still joins on PG.

    The bug report describes ``credentials.id`` as a native uuid and
    ``sync_runs.connection_id`` holding a **UUID-formatted string** — the
    representation the pre-0045 varchar(64) column and legacy backfill
    produced.  On the old schema the join ``credentials.id =
    sync_runs.connection_id`` raised the uuid = varchar operator error;
    after 0045 the asyncpg layer coerces the string into the uuid type and
    the join executes.  No casts are needed anywhere in the test.
    """
    tenant = Tenant(slug="sync-runs-stats-str", name="str")
    session.add(tenant)
    await session.flush()

    cred = Credential(
        tenant_id=tenant.id,
        provider_key="bunq",
        encrypted_payload=b"\x00" * 16,
        nonce=b"\x00" * 12,
        status="active",
    )
    session.add(cred)
    await session.flush()

    # Store the run's connection_id as a UUID-formatted *string*.
    session.add(
        SyncRun(
            connector="bunq",
            connection_id=str(cred.id),
            status=SyncRunStatus.COMPLETED,
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await session.commit()

    async with session_factory() as s:
        svc = ReadService(s)
        result = await svc.list_sync_runs(tenant_id=str(tenant.id))

    assert result.total == 1
    counts = {(c.connector, c.status): c.count for c in result.status_counts}
    assert counts[("bunq", "completed")] == 1
