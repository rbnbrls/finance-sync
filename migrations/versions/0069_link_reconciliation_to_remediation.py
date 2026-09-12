"""Link reconciliation findings to remediation backlog items."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reconciliation_results",
        sa.Column("remediation_item_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_reconciliation_results_remediation_item",
        "reconciliation_results",
        "data_quality_remediation_items",
        ["remediation_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_reconciliation_results_remediation_item_id",
        "reconciliation_results",
        ["remediation_item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reconciliation_results_remediation_item_id",
        table_name="reconciliation_results",
    )
    op.drop_constraint(
        "fk_reconciliation_results_remediation_item",
        "reconciliation_results",
        type_="foreignkey",
    )
    op.drop_column("reconciliation_results", "remediation_item_id")
