"""Add api_keys and credentials tables for auth and credential storage.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════
    # 1. api_keys — persistent machine-to-machine API keys
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("key_prefix", sa.String(8), nullable=False, index=True),
        sa.Column("key_hash", sa.String(256), nullable=False),
        sa.Column(
            "permissions",
            sa.Text,
            nullable=True,
            comment="Space-separated permission strings, e.g. "
            "'transactions:read transactions:write'",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_api_keys_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_api_keys_user_id_users",
        ),
        comment="Persistent API keys for machine clients",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. credentials — envelope-encrypted provider secrets
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=False,
            index=True,
            comment="Provider identifier, e.g. 'plaid', 'teller'",
        ),
        sa.Column(
            "encrypted_payload",
            sa.LargeBinary(),
            nullable=False,
            comment="AES-256-GCM ciphertext (includes 16-byte GCM auth tag)",
        ),
        sa.Column(
            "nonce",
            sa.LargeBinary(),
            nullable=False,
            comment="12-byte randomly generated nonce / IV",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Human-readable label",
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_credentials_tenant_id_tenants",
        ),
        comment="Envelope-encrypted provider credentials (AES-256-GCM)",
    )
    op.create_index(
        "ix_credentials_tenant_provider",
        "credentials",
        ["tenant_id", "provider_key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("credentials")
    op.drop_table("api_keys")
