"""Add sync cursor persistence (G-03).

The ``sync_cursor`` table has been documented in docs/DATABASE.md since
the initial design but was never migrated — the orchestrator always
defaulted to ``since = now - 90 days`` and incremental sync could not
resume from a stored position (roadmap gaps ms.2.f.2 / ms.2.ac.1).

- Create ``sync_cursor`` (tenant_id, connector, resource, cursor,
  created_at, updated_at) with a unique ``(tenant_id, connector,
  resource)`` constraint — one row per sync resource (e.g. per external
  account, or ``card_transactions`` for the cards pipeline).
- Add ``sync_runs.cursor`` — the watermark a successful run advanced
  to, set only on completion and exposed via ``GET /sync-runs``.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-14
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the sync_cursor table and the sync_runs.cursor column."""
    op.create_table(
        "sync_cursor",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "connector",
            sa.String(length=64),
            nullable=False,
            comment="Connector name, e.g. 'bunq', 'bunq_cards'",
        ),
        sa.Column(
            "resource",
            sa.String(length=128),
            nullable=False,
            comment=(
                "Sync resource, e.g. an external account id for "
                "transaction sync or 'card_transactions' for the cards "
                "pipeline"
            ),
        ),
        sa.Column(
            "cursor",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Watermark: the run start time of the last successful "
                "sync; the next run fetches transactions on or after "
                "this time"
            ),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_sync_cursor_tenant_id_tenants"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_cursor")),
        sa.UniqueConstraint(
            "tenant_id",
            "connector",
            "resource",
            name="uq_sync_cursor_tenant_connector_resource",
        ),
    )
    op.create_index(
        op.f("ix_sync_cursor_tenant_id"),
        "sync_cursor",
        ["tenant_id"],
        unique=False,
    )
    op.add_column(
        "sync_runs",
        sa.Column(
            "cursor",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Watermark this run advanced the sync cursor to (set on "
                "successful completion); NULL on failure"
            ),
        ),
    )


def downgrade() -> None:
    """Drop the cursor column and the sync_cursor table."""
    op.drop_column("sync_runs", "cursor")
    op.drop_index(op.f("ix_sync_cursor_tenant_id"), table_name="sync_cursor")
    op.drop_table("sync_cursor")
