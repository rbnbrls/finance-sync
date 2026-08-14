"""Add exporter tables: export_runs, ab_account_mappings, export_deliveries.

These tables back the export pipeline (ExportRun tracking, per-account
Actual Budget account mappings, and idempotent ExportDelivery cursors).
They were previously created only via ``Base.metadata.create_all`` in the
app lifespan — this revision brings them into the Alembic-managed schema.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════
    # 1. export_runs — outcome tracking for every export attempt
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "export_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'running'"),
            comment="'running', 'completed', 'failed', 'cancelled'",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("transactions_attempted", sa.Integer(), nullable=True),
        sa.Column("transactions_exported", sa.Integer(), nullable=True),
        sa.Column("transactions_failed", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        comment="Tracks a single export run for downstream alerting/dashboards",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. ab_account_mappings — finance-sync account -> Actual Budget account
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "ab_account_mappings",
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
            index=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
            comment="finance-sync account UUID",
        ),
        sa.Column(
            "ab_account_id",
            sa.String(64),
            nullable=False,
            comment="Actual Budget internal account UUID",
        ),
        sa.Column(
            "ab_account_name",
            sa.String(256),
            nullable=False,
            comment="Actual Budget account display name (cached)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "account_id",
            name="uq_ab_mapping_account",
        ),
        comment="Maps a finance-sync account to an Actual Budget account",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 3. export_deliveries — per-account idempotency cursor for exports
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "export_deliveries",
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
            index=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
            comment="finance-sync account UUID",
        ),
        sa.Column(
            "last_exported_transaction_id",
            sa.String(64),
            nullable=True,
            comment="ID of the last successfully exported transaction",
        ),
        sa.Column(
            "last_exported_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp of the last successful export for this account",
        ),
        sa.Column(
            "last_cursor",
            sa.Text(),
            nullable=True,
            comment="Provider cursor / checkpoint token for resume",
        ),
        sa.Column(
            "export_run_id",
            sa.String(64),
            nullable=True,
            comment="ID of the ExportRun that last updated this cursor",
        ),
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
            "account_id",
            name="uq_export_delivery_account",
        ),
        comment="Idempotency cursor: last successfully exported tx per account",
    )


def downgrade() -> None:
    """Drop exporter tables in reverse order."""
    op.drop_table("export_deliveries")
    op.drop_table("ab_account_mappings")
    op.drop_table("export_runs")
