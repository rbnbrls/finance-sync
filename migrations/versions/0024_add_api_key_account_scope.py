"""Add optional account scope to machine API keys.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("account_scope", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "account_scope")
