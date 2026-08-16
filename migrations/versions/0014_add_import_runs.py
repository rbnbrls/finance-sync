"""Add auditable import runs.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("batch_hash", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("report_types", postgresql.JSONB(), nullable=False),
        sa.Column("content_hashes", postgresql.JSONB(), nullable=False),
        sa.Column("file_names", postgresql.JSONB(), nullable=False),
        sa.Column("storage_names", postgresql.JSONB(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rows_total", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "skipped_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "rejected_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("error_details", postgresql.JSONB(), nullable=False),
        sa.Column("preview", postgresql.JSONB(), nullable=False),
        sa.Column("audit_events", postgresql.JSONB(), nullable=False),
        sa.Column(
            "retained", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "connection_id",
            "batch_hash",
            "attempt",
            name="uq_import_run_batch_attempt",
        ),
    )
    op.create_index("ix_import_runs_tenant_id", "import_runs", ["tenant_id"])
    op.create_index(
        "ix_import_runs_connection_id", "import_runs", ["connection_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_import_runs_connection_id", table_name="import_runs")
    op.drop_index("ix_import_runs_tenant_id", table_name="import_runs")
    op.drop_table("import_runs")
