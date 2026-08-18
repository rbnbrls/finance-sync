"""Add governed datamarts, consumers, and grants.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datamarts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column(
            "fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("delivery_method", sa.String(length=32), nullable=False),
        sa.Column(
            "delivery_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
        sa.UniqueConstraint("tenant_id", "key", name="uq_datamarts_tenant_key"),
    )
    op.create_index("ix_datamarts_tenant_id", "datamarts", ["tenant_id"])

    op.create_table(
        "datamart_consumers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
            "tenant_id", "key", name="uq_datamart_consumers_tenant_key"
        ),
        sa.UniqueConstraint("api_key_id"),
    )
    op.create_index(
        "ix_datamart_consumers_tenant_id", "datamart_consumers", ["tenant_id"]
    )

    op.create_table(
        "datamart_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "consumer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datamart_consumers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "datamart_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datamarts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "household_scope",
            sa.String(length=16),
            nullable=False,
            server_default="explicit",
        ),
        sa.Column(
            "allowed_account_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "allowed_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
            "consumer_id",
            "datamart_id",
            name="uq_datamart_grants_consumer_mart",
        ),
    )
    op.create_index(
        "ix_datamart_grants_tenant_id", "datamart_grants", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_datamart_grants_tenant_id", table_name="datamart_grants")
    op.drop_table("datamart_grants")
    op.drop_index(
        "ix_datamart_consumers_tenant_id", table_name="datamart_consumers"
    )
    op.drop_table("datamart_consumers")
    op.drop_index("ix_datamarts_tenant_id", table_name="datamarts")
    op.drop_table("datamarts")
