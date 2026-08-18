"""Scope exporter mappings and cursors by destination target.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_TABLES = (
    ("wealthfolio_account_mappings", "uq_wealthfolio_mapping_account"),
    ("wealthfolio_deliveries", "uq_wealthfolio_delivery_account"),
    ("ab_account_mappings", "uq_ab_mapping_account"),
    ("export_deliveries", "uq_export_delivery_account"),
)


def upgrade() -> None:
    for table, constraint in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "target_id",
                sa.String(length=64),
                nullable=False,
                server_default="legacy",
            ),
        )
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(
            constraint, table, ["tenant_id", "target_id", "account_id"]
        )


def downgrade() -> None:
    for table, constraint in reversed(_TABLES):
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(
            constraint, table, ["tenant_id", "account_id"]
        )
        op.drop_column(table, "target_id")
