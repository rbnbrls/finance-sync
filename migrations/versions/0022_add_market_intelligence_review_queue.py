"""Add market-intelligence review queue table.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-18

Implements the review queue for ambiguous security-identity resolutions
(story: "Bouw een legale self-hosted bronlaag voor portfolio-intelligence").
One entry per (tenant, item); idempotent on re-ingest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_intelligence_review_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "market_intelligence_items.id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("candidate_identifiers", postgresql.JSONB(), nullable=True),
        sa.Column(
            "resolution_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "resolved_security_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("securities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "tenant_id",
            "item_id",
            name="uq_market_intel_review_item",
        ),
    )
    op.create_index(
        "ix_market_intelligence_review_queue_tenant_id",
        "market_intelligence_review_queue",
        ["tenant_id"],
    )
    op.create_index(
        "ix_market_intelligence_review_queue_item_id",
        "market_intelligence_review_queue",
        ["item_id"],
    )


def downgrade() -> None:
    op.drop_table("market_intelligence_review_queue")
