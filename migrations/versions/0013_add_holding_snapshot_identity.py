"""Add the holding snapshot idempotency constraint.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Keep the oldest row if legacy code happened to insert the same snapshot
    # more than once, then enforce the ingestion idempotency key.
    op.execute(
        sa.text(
            """
            DELETE FROM holdings
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY tenant_id, account_id, security_id,
                                     observed_at, source
                        ORDER BY created_at, id
                    ) AS duplicate_number
                    FROM holdings
                ) ranked
                WHERE duplicate_number > 1
            )
            """
        )
    )
    op.create_unique_constraint(
        "uq_holdings_snapshot",
        "holdings",
        ["tenant_id", "account_id", "security_id", "observed_at", "source"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_holdings_snapshot", "holdings", type_="unique")
