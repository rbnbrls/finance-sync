"""Add fundamental_observations and security_metadata_observations tables
for Phase 3 fundamentals and ETF metadata enrichment.

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
    # ═══════════════════════════════════════════════════════════════════
    # 1. fundamental_observations — point-in-time fundamental ratios
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "fundamental_observations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
            comment="FK to securities.id",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="When the fundamental observation was recorded",
        ),
        # Valuation ratios
        sa.Column(
            "pe_ratio",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Price-to-Earnings ratio (TTM)",
        ),
        sa.Column(
            "forward_pe",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Forward Price-to-Earnings ratio",
        ),
        sa.Column(
            "peg_ratio",
            sa.Numeric(20, 6),
            nullable=True,
            comment="PE / Growth ratio",
        ),
        # Per-share metrics
        sa.Column(
            "eps",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Earnings Per Share (TTM)",
        ),
        sa.Column(
            "eps_forward",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Forward EPS estimate",
        ),
        sa.Column(
            "book_value_per_share",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Book Value Per Share",
        ),
        # Dividend
        sa.Column(
            "dividend_yield",
            sa.Numeric(20, 8),
            nullable=True,
            comment="Dividend yield as decimal (e.g. 0.035 = 3.5%)",
        ),
        sa.Column(
            "dividend_rate",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Annual dividend rate per share",
        ),
        # Size & liquidity
        sa.Column(
            "market_cap",
            sa.Numeric(30, 6),
            nullable=True,
            comment="Market capitalisation in base currency",
        ),
        sa.Column(
            "enterprise_value",
            sa.Numeric(30, 6),
            nullable=True,
            comment="Enterprise value",
        ),
        sa.Column(
            "shares_outstanding",
            sa.Numeric(30, 6),
            nullable=True,
            comment="Number of shares outstanding",
        ),
        # Risk & volatility
        sa.Column(
            "beta",
            sa.Numeric(10, 6),
            nullable=True,
            comment="Beta (5-year monthly, vs benchmark)",
        ),
        # 52-week range
        sa.Column(
            "high_52w",
            sa.Numeric(20, 6),
            nullable=True,
            comment="52-week high price",
        ),
        sa.Column(
            "low_52w",
            sa.Numeric(20, 6),
            nullable=True,
            comment="52-week low price",
        ),
        # Metadata
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="Data source identifier (e.g. 'openbb', 'manual')",
        ),
        sa.Column(
            "provider_metadata",
            postgresql.JSONB,
            nullable=True,
            comment="Provider-specific additional metadata",
        ),
        # TimestampMixin
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
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="CASCADE",
            name="fk_fundamental_observations_security_id_securities",
        ),
        sa.UniqueConstraint(
            "security_id",
            "timestamp",
            "source",
            name="uq_fundamental_obs_ts_source",
        ),
        comment="Point-in-time fundamental metric observations for securities",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. security_metadata_observations — structured metadata payloads
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "security_metadata_observations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
            comment="FK to securities.id",
        ),
        sa.Column(
            "metadata_type",
            sa.String(64),
            nullable=False,
            index=True,
            comment="Discriminator: etf_composition, sector_exposure, "
            "fundamental_ratios, company_profile",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="When the metadata observation was recorded",
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Arbitrary structured metadata payload",
        ),
        sa.Column(
            "label",
            sa.String(256),
            nullable=True,
            comment="Human-readable label "
            "(e.g. ETF name, sector title)",
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="Data source identifier (e.g. 'openbb', 'manual')",
        ),
        # TimestampMixin
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
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="CASCADE",
            name="fk_sec_metadata_obs_security_id_securities",
        ),
        sa.UniqueConstraint(
            "security_id",
            "metadata_type",
            "timestamp",
            "source",
            name="uq_sec_metadata_obs_type_ts_source",
        ),
        comment="Point-in-time structured metadata observations for securities",
    )


def downgrade() -> None:
    op.drop_table("security_metadata_observations")
    op.drop_table("fundamental_observations")
