"""Add reconciliation tables (reconciliation_runs, reconciliation_results).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── reconciliation_runs ──────────────────────────────────────────
    op.create_table(
        "reconciliation_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
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
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "scope",
            postgresql.JSONB,
            nullable=True,
            comment=(
                "Run scope: {account_ids: [..], "
                "date_from: '..', date_to: '..'}"
            ),
        ),
        sa.Column(
            "finding_count",
            sa.Integer,
            nullable=True,
            comment="Total number of findings in this run",
        ),
        sa.Column(
            "summary",
            postgresql.JSONB,
            nullable=True,
            comment=(
                "Summary stats: {duplicates: N, missing: N, "
                "cross_connector: N, by_severity: {info: N, ...}}"
            ),
        ),
        sa.Column(
            "error_message",
            sa.Text,
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ── reconciliation_results ───────────────────────────────────────
    op.create_table(
        "reconciliation_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "reconciliation_runs.id", ondelete="CASCADE"
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment=(
                "'duplicate_transaction', 'missing_transaction', "
                "'cross_connector_mismatch', 'amount_mismatch'"
            ),
        ),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'warning'"),
            comment="'info', 'warning', 'error'",
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=True,
            comment="Primary connector involved",
        ),
        sa.Column(
            "other_provider_key",
            sa.String(64),
            nullable=True,
            comment="Secondary connector (cross-connector context)",
        ),
        sa.Column(
            "transaction_id_a",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="First (or only) transaction involved",
        ),
        sa.Column(
            "transaction_id_b",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Second transaction (for duplicates/mismatches)",
        ),
        sa.Column(
            "external_transaction_id_a",
            sa.String(256),
            nullable=True,
        ),
        sa.Column(
            "external_transaction_id_b",
            sa.String(256),
            nullable=True,
        ),
        sa.Column(
            "amount",
            sa.Numeric(24, 8),
            nullable=True,
        ),
        sa.Column(
            "other_amount",
            sa.Numeric(24, 8),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "description",
            sa.String(512),
            nullable=True,
        ),
        sa.Column(
            "details",
            postgresql.JSONB,
            nullable=True,
            comment="Extra context (score, diff, etc.)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Individual reconciliation findings per run",
    )


def downgrade() -> None:
    op.drop_table("reconciliation_results")
    op.drop_table("reconciliation_runs")
