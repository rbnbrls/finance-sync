"""Add Wealthfolio delivery cursor table + export_runs.exporter_type.

Gap G-14: the Wealthfolio exporter previously had no delivery cursor,
so pushes could not resume idempotently after a partial failure.  This
revision adds ``wealthfolio_deliveries`` (a per-account cursor mirroring
``export_deliveries`` but kept separate so both exporters can track
independent cursors for the same finance-sync account) and records which
exporter ran each ``export_runs`` row (``exporter_type``) so the API can
expose failed runs per exporter and validate retries.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════
    # 1. wealthfolio_deliveries — per-account idempotency cursor for
    #    pushes to a Wealthfolio instance (G-14)
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "wealthfolio_deliveries",
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
            comment="ID of the last successfully pushed transaction",
        ),
        sa.Column(
            "last_exported_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp of the last successful push for this account",
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
            name="uq_wealthfolio_delivery_account",
        ),
        comment=(
            "Idempotency cursor: last successfully pushed tx per account "
            "for the Wealthfolio exporter"
        ),
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. export_runs.exporter_type — which exporter ran this run
    # ═══════════════════════════════════════════════════════════════════
    op.add_column(
        "export_runs",
        sa.Column(
            "exporter_type",
            sa.String(32),
            nullable=True,
            comment="Exporter key ('wealthfolio', 'actual-budget') that ran this",
        ),
    )


def downgrade() -> None:
    """Drop the Wealthfolio delivery cursor and exporter_type column."""
    op.drop_column("export_runs", "exporter_type")
    op.drop_table("wealthfolio_deliveries")
