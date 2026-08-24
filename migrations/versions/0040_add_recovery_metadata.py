"""Add sync/export recovery metadata for the control plane."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_runs",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("export_runs", sa.Column("error_category", sa.String(32)))
    op.add_column(
        "export_runs",
        sa.Column("target_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "export_runs",
        sa.Column(
            "account_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "export_runs",
        sa.Column(
            "delivery_checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_index("ix_export_runs_target_id", "export_runs", ["target_id"])


def downgrade() -> None:
    op.drop_index("ix_export_runs_target_id", table_name="export_runs")
    op.drop_column("export_runs", "delivery_checkpoint")
    op.drop_column("export_runs", "account_scope")
    op.drop_column("export_runs", "target_id")
    op.drop_column("export_runs", "error_category")
    op.drop_column("sync_runs", "warnings")
