"""Add explicit credential lifecycle and expiry metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credentials", sa.Column("credential_status", sa.String(24), nullable=False, server_default="unknown"))
    op.add_column("credentials", sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("credentials", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("credentials", sa.Column("reauth_required_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("credentials", sa.Column("last_auth_error_code", sa.String(64), nullable=True))
    op.add_column("credentials", sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    for column in ("credential_version", "last_auth_error_code", "reauth_required_at", "expires_at", "last_authenticated_at", "credential_status"):
        op.drop_column("credentials", column)
