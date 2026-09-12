"""PostgreSQL behavioral contract for the Phase 01 cursor migration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import run_alembic

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _require_redis(redis_client) -> None:  # pyright: ignore[reportUnusedFunction]
    """Keep migration validation explicit when Redis is unavailable."""
    await redis_client.ping()


async def test_health_cursor_schema_is_tenant_and_target_scoped(
    session: AsyncSession,
) -> None:
    """The migrated cursor table stores bounded aggregate state and indexes."""
    table = await session.execute(
        sa.text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'wealthfolio_health_cursors'
            """
        )
    )
    columns = {row[0] for row in table}
    assert columns >= {
        "tenant_id",
        "target_id",
        "last_successful_poll",
        "payload_hash",
        "issue_count",
        "complete",
        "truncated",
        "cursor_state",
        "last_error",
        "last_error_category",
    }

    indexes = await session.execute(
        sa.text(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'wealthfolio_health_cursors'
            """
        )
    )
    index_names = {row[0] for row in indexes}
    assert "uq_wealthfolio_health_cursor_tenant_target" in index_names
    assert "ix_wealthfolio_health_cursor_tenant_success" in index_names
    assert "ix_wealthfolio_health_cursor_target" in index_names

    constraints = await session.execute(
        sa.text(
            """
            SELECT conname, contype, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'public.wealthfolio_health_cursors'::regclass
            """
        )
    )
    constraint_rows = list(constraints)
    assert (
        "uq_wealthfolio_health_cursor_tenant_target",
        "UNIQUE (tenant_id, target_id)",
    ) in {(row[0], row[2]) for row in constraint_rows if row[1] == "u"}
    assert any(row[1] == "f" and "tenants" in row[2] for row in constraint_rows)
    assert any(
        row[1] == "f" and "export_targets" in row[2] for row in constraint_rows
    )


async def test_phase01_cursor_migrations_reach_head_idempotently(
    fresh_database_url: str,
) -> None:
    """0071 and 0072 apply on an empty DB and remain safe on an existing DB."""
    run_alembic("upgrade", "head", url=fresh_database_url)
    run_alembic("upgrade", "head", url=fresh_database_url)

    engine = create_async_engine(fresh_database_url)
    try:
        async with engine.connect() as conn:
            revision = (
                await conn.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                )
            ).scalar_one()
            assert revision == "0072"
            cursor_count = (
                await conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'wealthfolio_health_cursors'"
                    )
                )
            ).scalar_one()
            assert cursor_count == 1
    finally:
        await engine.dispose()
