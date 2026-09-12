"""PostgreSQL behavioral contract for the Phase 01 cursor migration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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
