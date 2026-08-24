"""Scope control-plane records by tenant.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def _backfill_or_fail(table: str) -> None:
    remaining = (
        op.get_bind()
        .execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE tenant_id IS NULL")
        )
        .scalar_one()
    )
    if remaining:
        message = (
            f"Cannot safely tenant-scope {table}: {remaining} legacy rows "
            "have no unambiguous tenant mapping"
        )
        raise RuntimeError(message)


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.add_column(
        "export_runs",
        sa.Column("tenant_id", uuid_type, nullable=True),
    )
    op.add_column(
        "unresolved_securities",
        sa.Column("tenant_id", uuid_type, nullable=True),
    )
    op.add_column(
        "resolution_audit_log",
        sa.Column("tenant_id", uuid_type, nullable=True),
    )

    # Delivery cursors are the only reliable legacy relation from an export
    # run to a tenant. Runs without one are deliberately rejected below.
    op.execute(
        sa.text(
            """
            UPDATE export_runs AS r
            SET tenant_id = source.tenant_id
            FROM (
                SELECT export_run_id, (array_agg(tenant_id))[1] AS tenant_id
                FROM (
                    SELECT export_run_id, tenant_id FROM wealthfolio_deliveries
                    WHERE export_run_id IS NOT NULL
                    UNION ALL
                    SELECT export_run_id, tenant_id FROM export_deliveries
                    WHERE export_run_id IS NOT NULL
                ) AS deliveries
                GROUP BY export_run_id
                HAVING count(DISTINCT tenant_id) = 1
            ) AS source
            WHERE r.id::text = source.export_run_id
            """
        )
    )

    # A provider is only safe as a legacy tenant key when it belongs to one
    # tenant. Ambiguous providers remain for an operator-led migration.
    op.execute(
        sa.text(
            """
            UPDATE unresolved_securities AS u
            SET tenant_id = source.tenant_id
            FROM (
                SELECT u2.id, (array_agg(c.tenant_id))[1] AS tenant_id
                FROM unresolved_securities AS u2
                JOIN credentials AS c ON c.provider_key = u2.provider_key
                GROUP BY u2.id
                HAVING count(DISTINCT c.tenant_id) = 1
            ) AS source
            WHERE u.id = source.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE resolution_audit_log AS a
            SET tenant_id = u.tenant_id
            FROM unresolved_securities AS u
            WHERE a.unresolved_security_id = u.id
            """
        )
    )

    _backfill_or_fail("export_runs")
    _backfill_or_fail("unresolved_securities")

    op.alter_column("export_runs", "tenant_id", nullable=False)
    op.alter_column("unresolved_securities", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_export_runs_tenant_id_tenants",
        "export_runs",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_unresolved_securities_tenant_id_tenants",
        "unresolved_securities",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_resolution_audit_log_tenant_id_tenants",
        "resolution_audit_log",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_index("ix_export_runs_tenant_id", "export_runs", ["tenant_id"])
    op.create_index(
        "ix_resolution_audit_log_tenant_id",
        "resolution_audit_log",
        ["tenant_id"],
    )

    op.drop_constraint(
        "uq_unresolved_provider_ext_id",
        "unresolved_securities",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_unresolved_provider_ext_id",
        "unresolved_securities",
        ["tenant_id", "provider_key", "external_security_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_unresolved_provider_ext_id",
        "unresolved_securities",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_unresolved_provider_ext_id",
        "unresolved_securities",
        ["provider_key", "external_security_id"],
    )
    op.drop_index(
        "ix_resolution_audit_log_tenant_id", table_name="resolution_audit_log"
    )
    op.drop_index("ix_export_runs_tenant_id", table_name="export_runs")
    op.drop_constraint(
        "fk_resolution_audit_log_tenant_id_tenants",
        "resolution_audit_log",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_unresolved_securities_tenant_id_tenants",
        "unresolved_securities",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_export_runs_tenant_id_tenants", "export_runs", type_="foreignkey"
    )
    op.drop_column("resolution_audit_log", "tenant_id")
    op.drop_column("unresolved_securities", "tenant_id")
    op.drop_column("export_runs", "tenant_id")
