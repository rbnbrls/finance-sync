"""Add provider-neutral spending metadata, provenance and split entities."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


_transaction_fields = (
    ("provider_metadata_contract", postgresql.JSONB()),
    ("merchant_name", sa.String(256)),
    ("merchant_id", sa.String(256)),
    ("merchant_city", sa.String(128)),
    ("merchant_country", sa.String(64)),
    ("counterparty_name", sa.String(256)),
    ("counterparty_account_reference", sa.String(256)),
    ("merchant_category_code", sa.String(8)),
    ("original_type", sa.String(64)),
    ("original_status", sa.String(64)),
    ("authorization_status", sa.String(32)),
    ("settlement_status", sa.String(32)),
    ("source_record_hash", sa.String(128)),
    ("cashflow_bucket", sa.String(32)),
    ("cashflow_suggestion", postgresql.JSONB()),
    ("classification_source", sa.String(64)),
    ("classification_override", sa.String(64)),
    ("gross_amount", sa.Numeric(24, 8)),
    ("gross_currency_code", sa.String(3)),
    ("net_amount", sa.Numeric(24, 8)),
    ("net_currency_code", sa.String(3)),
    ("tax_amount", sa.Numeric(24, 8)),
    ("tax_currency_code", sa.String(3)),
    ("refund_amount", sa.Numeric(24, 8)),
    ("refund_currency_code", sa.String(3)),
)


def upgrade() -> None:
    for name, column_type in _transaction_fields:
        op.add_column(
            "transactions", sa.Column(name, column_type, nullable=True)
        )
    op.add_column(
        "accounts", sa.Column("capabilities", postgresql.JSONB(), nullable=True)
    )

    def common() -> list[sa.Column[object]]:
        return [
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "tenant_id", postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ]

    op.create_table(
        "merchant_identities",
        *common(),
        sa.Column("stable_key", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("aliases", postgresql.JSONB(), nullable=True),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("mccs", postgresql.JSONB(), nullable=True),
        sa.Column(
            "normalization_version",
            sa.String(32),
            nullable=False,
            server_default="1",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint(
            "tenant_id", "stable_key", name="uq_merchant_identity_key"
        ),
    )
    for table in (
        "transaction_source_references",
        "transaction_splits",
        "transaction_annotations",
    ):
        op.create_table(
            table,
            *common(),
            sa.Column(
                "transaction_id", postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(
                ["transaction_id"], ["transactions.id"], ondelete="CASCADE"
            ),
        )
        op.create_index(f"ix_{table}_transaction_id", table, ["transaction_id"])
    op.add_column(
        "transaction_source_references",
        sa.Column("object_type", sa.String(64), nullable=False),
    )
    op.add_column(
        "transaction_source_references",
        sa.Column(
            "external_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "transaction_source_references",
        sa.Column("provider_revisions", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "transaction_source_references",
        sa.Column("provider_metadata", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "transaction_splits",
        sa.Column("amount", sa.Numeric(24, 8), nullable=False),
    )
    op.add_column(
        "transaction_splits",
        sa.Column("currency_code", sa.String(3), nullable=False),
    )
    op.add_column(
        "transaction_splits",
        sa.Column("percentage", sa.Numeric(8, 5), nullable=True),
    )
    op.add_column(
        "transaction_splits",
        sa.Column("category_suggestion", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "transaction_splits",
        sa.Column("destination", sa.String(128), nullable=True),
    )
    op.add_column(
        "transaction_splits",
        sa.Column(
            "provenance", sa.String(64), nullable=False, server_default="user"
        ),
    )
    for name, typ in (
        ("annotation_type", sa.String(32)),
        ("content_hash", sa.String(128)),
        ("mime_type", sa.String(128)),
        ("safe_reference", sa.String(1024)),
        ("owner", sa.String(128)),
        ("retention_until", sa.DateTime(timezone=True)),
        ("destination_reference", sa.String(256)),
    ):
        op.add_column(
            "transaction_annotations",
            sa.Column(name, typ, nullable=name != "annotation_type"),
        )


def downgrade() -> None:
    for table in (
        "transaction_annotations",
        "transaction_splits",
        "transaction_source_references",
    ):
        op.drop_index(f"ix_{table}_transaction_id", table_name=table)
        op.drop_table(table)
    op.drop_table("merchant_identities")
    op.drop_column("accounts", "capabilities")
    for name, _ in reversed(_transaction_fields):
        op.drop_column("transactions", name)
