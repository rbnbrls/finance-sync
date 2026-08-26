"""Persist safe provider rate-limit diagnosis metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credentials", sa.Column("rate_limited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("credentials", sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("credentials", sa.Column("rate_limit_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("credentials", sa.Column("rate_limit_scope", sa.String(16), nullable=True))
    op.add_column("credentials", sa.Column("last_http_status", sa.Integer(), nullable=True))
    op.add_column("sync_runs", sa.Column("resource", sa.String(64), nullable=True))
    op.add_column("sync_runs", sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sync_runs", sa.Column("rate_limit_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sync_runs", sa.Column("rate_limit_scope", sa.String(16), nullable=True))
    op.add_column("sync_runs", sa.Column("last_http_status", sa.Integer(), nullable=True))


def downgrade() -> None:
    for column in ("last_http_status", "rate_limit_scope", "rate_limit_attempts", "retry_after_at", "resource"):
        op.drop_column("sync_runs", column)
    for column in ("last_http_status", "rate_limit_scope", "rate_limit_attempts", "retry_after_at", "rate_limited_at"):
        op.drop_column("credentials", column)
