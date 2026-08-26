"""Add controlled connector release state and promotion metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_releases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="candidate"
        ),
        sa.Column("previous_version", sa.String(32), nullable=True),
        sa.Column(
            "certification_status",
            sa.String(24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("certification_commit", sa.String(128), nullable=True),
        sa.Column(
            "compatibility_status",
            sa.String(24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "canary_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_key", "version", name="uq_connector_release_version"
        ),
    )
    op.create_index(
        "ix_connector_releases_provider_key",
        "connector_releases",
        ["provider_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connector_releases_provider_key", table_name="connector_releases"
    )
    op.drop_table("connector_releases")
