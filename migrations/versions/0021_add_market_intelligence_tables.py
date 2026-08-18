"""Add market-intelligence source layer tables.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-18

Implements the "legale self-hosted bronlaag voor portfolio-intelligence"
story: a provider-independent observation store (``market_intelligence_items``)
and per-provider run/freshness state (``market_intelligence_provider_states``).

Deduplication: items are unique on ``(tenant_id, provider, source_id)``
and on ``(tenant_id, content_hash)`` so syndicated duplicates collapse
regardless of which provider surfaced them first.

Licensing: the ``license_class`` column records the reuse class of the
source; the ingestion service enforces that only permissive classes may
persist ``body`` (full text) and that ``summary`` stays a short snippet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_intelligence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "valid_from", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "valid_until", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "language",
            sa.String(length=16),
            nullable=False,
            server_default="en",
        ),
        sa.Column("license_class", sa.String(length=32), nullable=False),
        sa.Column("license_uri", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("facts", postgresql.JSONB(), nullable=True),
        sa.Column("provider_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("identifiers", postgresql.JSONB(), nullable=True),
        sa.Column(
            "resolution_status",
            sa.String(length=32),
            nullable=False,
            server_default="unresolved",
        ),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("securities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
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
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "source_id",
            name="uq_market_intel_provider_source",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "content_hash",
            name="uq_market_intel_content_hash",
        ),
    )
    op.create_index(
        "ix_market_intelligence_items_tenant_id",
        "market_intelligence_items",
        ["tenant_id"],
    )
    op.create_index(
        "ix_market_intelligence_items_provider",
        "market_intelligence_items",
        ["provider"],
    )
    op.create_index(
        "ix_market_intelligence_items_published_at",
        "market_intelligence_items",
        ["published_at"],
    )
    op.create_index(
        "ix_market_intelligence_items_content_hash",
        "market_intelligence_items",
        ["content_hash"],
    )
    op.create_index(
        "ix_market_intelligence_items_security_id",
        "market_intelligence_items",
        ["security_id"],
    )

    op.create_table(
        "market_intelligence_provider_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "last_run_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_success_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_class", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("items_ingested", sa.Integer(), nullable=True),
        sa.Column("quota_used", sa.Integer(), nullable=True),
        sa.Column("quota_limit", sa.Integer(), nullable=True),
        sa.Column("freshness_max_age_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "freshness_min_interval_seconds", sa.Integer(), nullable=True
        ),
        sa.Column("capabilities", postgresql.JSONB(), nullable=True),
        sa.Column("availability", postgresql.JSONB(), nullable=True),
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
            "provider",
            name="uq_market_intel_provider_state",
        ),
    )
    op.create_index(
        "ix_market_intelligence_provider_states_tenant_id",
        "market_intelligence_provider_states",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_table("market_intelligence_provider_states")
    op.drop_table("market_intelligence_items")
