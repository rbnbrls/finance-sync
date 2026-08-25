"""Add normalized connection test metadata for control-plane projections."""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "credentials", sa.Column("last_error_category", sa.String(32))
    )
    op.add_column(
        "credentials",
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("credentials", sa.Column("last_test_status", sa.String(16)))
    op.add_column("credentials", sa.Column("last_test_error", sa.Text()))


def downgrade() -> None:
    op.drop_column("credentials", "last_test_error")
    op.drop_column("credentials", "last_test_status")
    op.drop_column("credentials", "last_test_at")
    op.drop_column("credentials", "last_error_category")
