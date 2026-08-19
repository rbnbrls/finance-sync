"""Add append-only run registry for market-intelligence refreshes.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-19

Implements the scheduler story requirement that every provider run is
recorded (started/completed, duration, quota, freshness, sanitised
errors).  ``market_intelligence_provider_states`` keeps the *latest*
run for cadence decisions; this new ``market_intelligence_runs`` table
carries the full observable history (append-only, never updated in
place).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_intelligence_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the scheduler run started",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the run completed (null while still running)",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
            comment="pending/ok/degraded/unavailable",
        ),
        sa.Column(
            "forced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True when the run was forced (ignored cadence)",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("items_ingested", sa.Integer(), nullable=True),
        sa.Column("quota_used", sa.Integer(), nullable=True),
        sa.Column("quota_limit", sa.Integer(), nullable=True),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="Sanitised error message (secrets redacted)",
        ),
        sa.Column(
            "error_class",
            sa.String(length=64),
            nullable=True,
            comment="Exception class name of the failure",
        ),
        sa.Column(
            "freshness_max_age_seconds",
            sa.Integer(),
            nullable=True,
            comment="Provider freshness max-age at run time (seconds)",
        ),
        sa.Column(
            "freshness_min_interval_seconds",
            sa.Integer(),
            nullable=True,
            comment="Provider min re-fetch interval at run time (seconds)",
        ),
        sa.Column(
            "capabilities",
            postgresql.JSONB(),
            nullable=True,
            comment="Capabilities advertised by the provider at run time",
        ),
        sa.Column(
            "availability",
            postgresql.JSONB(),
            nullable=True,
            comment="Capability → availability mapping at run time",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_market_intelligence_runs_tenant_id",
        "market_intelligence_runs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_market_intel_runs_tenant_provider_started",
        "market_intelligence_runs",
        ["tenant_id", "provider", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_intel_runs_tenant_provider_started",
        table_name="market_intelligence_runs",
    )
    op.drop_index(
        "ix_market_intelligence_runs_tenant_id",
        table_name="market_intelligence_runs",
    )
    op.drop_table("market_intelligence_runs")
