"""Remove the retired household-sharing interface.

The single-owner datalake model no longer exposes household invitations,
members or account sharing.  This migration drops the household tables and
the account ``visibility`` column that only the sharing feature used.

``owner_user_id`` stays: it remains a provenance chain (user → connection
→ account) for the canonical datalake, not a sharing boundary.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_household_audit_log_tenant_id", table_name="household_audit_log"
    )
    op.drop_index(
        "ix_household_audit_tenant_created", table_name="household_audit_log"
    )
    op.drop_table("household_audit_log")

    op.drop_index(
        "ix_household_invitations_tenant_id",
        table_name="household_invitations",
    )
    op.drop_index(
        "ix_household_invitations_token_hash",
        table_name="household_invitations",
    )
    op.drop_index(
        "ix_household_invitations_tenant_status",
        table_name="household_invitations",
    )
    op.drop_table("household_invitations")

    op.drop_column("accounts", "visibility")


def downgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "visibility",
            sa.String(length=16),
            server_default="private",
            nullable=False,
        ),
    )
    op.create_table(
        "household_invitations",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("accepted_by", sa.String(length=64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_household_invitations_tenant_status",
        "household_invitations",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_household_invitations_token_hash",
        "household_invitations",
        ["token_hash"],
    )
    op.create_index(
        "ix_household_invitations_tenant_id",
        "household_invitations",
        ["tenant_id"],
    )
    op.create_table(
        "household_audit_log",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column(
            "detail",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_household_audit_tenant_created",
        "household_audit_log",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_household_audit_log_tenant_id",
        "household_audit_log",
        ["tenant_id"],
    )
