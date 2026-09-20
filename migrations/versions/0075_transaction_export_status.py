"""Add the canonical downstream export status to transactions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "export_status" not in columns:
        op.add_column(
            "transactions",
            sa.Column(
                "export_status",
                sa.String(length=32),
                nullable=False,
                server_default="active",
            ),
        )

    op.execute(
        sa.text(
            """
            UPDATE transactions
            SET export_status = 'excluded'
            WHERE provider_key = 'bunq'
              AND (
                provider_metadata->>'internal_transfer_detection'
                  = 'bunq_easy_budgeting'
                OR lower(trim(description)) IN (
                  'remainder of your inbox budget.',
                  'remainder of your outbox budget.',
                  'automatic budget top up.',
                  'automatic top-up of your inbox budget.',
                  'automatic top-up of your outbox budget.'
                )
              )
            """
        )
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "export_status" in columns:
        op.drop_column("transactions", "export_status")
