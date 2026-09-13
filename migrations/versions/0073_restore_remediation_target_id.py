"""Restore the remediation target identity column for upgraded databases."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some local databases were stamped past 0068 without the column that is
    # present in the ORM model.  Keep this migration idempotent so both clean
    # and upgraded installations can safely converge.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("data_quality_remediation_items")
    }
    if "target_id" not in columns:
        op.add_column(
            "data_quality_remediation_items",
            sa.Column(
                "target_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
        )
        op.create_foreign_key(
            "fk_dq_remediation_target_id",
            "data_quality_remediation_items",
            "export_targets",
            ["target_id"],
            ["id"],
            ondelete="SET NULL",
        )
    indexes = {
        index["name"]
        for index in inspector.get_indexes("data_quality_remediation_items")
    }
    if "ix_dq_remediation_target" not in indexes:
        op.create_index(
            "ix_dq_remediation_target",
            "data_quality_remediation_items",
            ["target_id"],
        )
    op.execute(
        sa.text(
            """
            UPDATE data_quality_remediation_items AS item
            SET target_id = NULLIF(item.context->>'target_id', '')::uuid
            WHERE item.target_id IS NULL
              AND jsonb_typeof(item.context->'target_id') = 'string'
              AND (item.context->>'target_id') ~* '^[0-9a-f-]{36}$'
            """
        )
    )


def downgrade() -> None:
    # 0068 already creates target_id on clean databases.  This migration is
    # therefore also used to repair upgraded installations where only the
    # index was missing; never remove the column owned by 0068 on downgrade.
    inspector = sa.inspect(op.get_bind())
    indexes = {
        index["name"]
        for index in inspector.get_indexes("data_quality_remediation_items")
    }
    if "ix_dq_remediation_target" in indexes:
        op.drop_index(
            "ix_dq_remediation_target",
            table_name="data_quality_remediation_items",
        )
