"""Retain provider facts on canonical transactions for repair and audit."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("transactions")
    }
    if "provider_metadata" not in columns:
        op.add_column(
            "transactions",
            sa.Column(
                "provider_metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("transactions")
    }
    if "provider_metadata" in columns:
        op.drop_column("transactions", "provider_metadata")
