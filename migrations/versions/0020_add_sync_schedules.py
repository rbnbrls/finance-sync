"""Add tenant-scoped sync schedules (per-connection / per-exporter).

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-18

Implements the "configureerbare syncschema's per connector en exporter"
story: every active ingestion connection and every configured export
target receives an enabled default schedule (weekdays Mon-Fri 07:00 in
the tenant timezone, falling back to ``UTC`` when the tenant has no
timezone).  The worker plans runs exclusively from these rows going
forward; the global ``WORKER_JOB_*`` intervals remain operational
limits.

Backfill semantics
------------------

* **Ingestion** — one schedule per *active* ``credentials`` row whose
  ``provider_key`` is schedulable (``bunq``, ``trading212``).  The
  ``target_id`` is the credential/connection id.
* **Export** — exporters are configured globally via environment, so a
  tenant is treated as having a *configured export target* when it has
  per-tenant export state: rows in ``wealthfolio_deliveries`` or
  ``wealthfolio_account_mappings`` (→ exporter ``wealthfolio``), or
  ``export_deliveries`` or ``ab_account_mappings`` (→ exporter
  ``actual-budget``).  The union is deduplicated so at most one schedule
  per (tenant, exporter) is created.
* **next_run_at** — the first future instant (next weekday 07:00 in
  ``Europe/Amsterdam``) is computed **in SQL** so backfilled rows are
  immediately schedulable.  Because it always points strictly into the
  future, the migration itself never fires a run on migration day:
  existing global jobs therefore cause no unexpected extra run, and the
  worker only executes when the computed instant arrives.
* **Idempotency** — ``INSERT ... ON CONFLICT DO NOTHING`` keyed on the
  unique ``(tenant_id, scope, target_id)`` constraint, so a re-run or a
  crash/retry mid-migration converges to exactly one row per scope.
  Historical duplicate rows are impossible after the constraint is
  created.

Time zone: the ``tenants`` table currently has no timezone column, so
every schedule is created with ``Europe/Amsterdam`` (the documented
tenant-default in ``models/sync_schedule.py``).  When a per-tenant
timezone is added later, this literal becomes a subselect from
``tenants``.

Revision ID: 0020
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Default schedule JSON — weekdays (Mon-Fri) at 07:00, schema v1.
_DEFAULT_SCHEDULE_SQL = (
    "'{\"frequency\": \"weekdays\", \"time\": \"07:00\", "
    "\"weekdays\": [0, 1, 2, 3, 4]}'::jsonb"
)
#: Tenant timezone default (the ``tenants`` table has no timezone column).
_TENANT_TZ = "Europe/Amsterdam"
#: Next future instant: first weekday (Mon-Fri) at 07:00 local, strictly
#: after ``now()``.  ``AT TIME ZONE`` handles DST automatically (07:00
#: always exists — spring-forward only skips 02:00-03:00).
_NEXT_RUN_SQL = (
    "("
    "SELECT (d + time '07:00') AT TIME ZONE 'Europe/Amsterdam' "
    "FROM generate_series(0, 6) AS g(offset_days) "
    "CROSS JOIN LATERAL (SELECT (now()::date + g.offset_days)::date AS d) dd "
    "WHERE extract(isodow FROM d) <= 5 "
    "AND (d + time '07:00') AT TIME ZONE 'Europe/Amsterdam' > now() "
    "ORDER BY d LIMIT 1"
    ")"
)

#: Ingestion providers that historically ran on the global worker jobs
#: (bunq transaction sync + bunq cards + Trading212) and therefore get a
#: default schedule.  Other providers (e.g. degiro_pension watchfolder
#: imports, csv_import, manual_expense) keep their own triggers.
_INGESTION_PROVIDERS = ("bunq", "trading212")

#: Exporters that receive a default schedule.
_EXPORT_EXPORTERS = ("wealthfolio", "actual-budget")


def _insert_default_schedule_sql(
    *,
    scope: str,
    target_expr: str,
    source_sql: str,
) -> str:
    """Build the idempotent INSERT … ON CONFLICT DO NOTHING statement.

    *source_sql* is a SELECT over a relation that exposes ``tenant_id``
    and (via *target_expr*) the target id — the FROM alias is always
    ``t``.
    """
    return f"""
INSERT INTO sync_schedules (
    id, tenant_id, scope, target_id, enabled, schedule, schema_version,
    timezone, version, next_run_at, last_scheduled_at, last_run_at,
    last_run_status, last_run_error, created_by, updated_by, created_at,
    updated_at
)
SELECT
    gen_random_uuid(), t.tenant_id, '{scope}', {target_expr},
    TRUE, {_DEFAULT_SCHEDULE_SQL}, 1,
    '{_TENANT_TZ}', 1, {_NEXT_RUN_SQL}, NULL, NULL, NULL, NULL, NULL, NULL,
    now(), now()
FROM ({source_sql}) AS t
ON CONFLICT (tenant_id, scope, target_id) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "sync_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "schedule",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_scheduled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_run_status", sa.String(length=16), nullable=True),
        sa.Column("last_run_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "target_id",
            name="uq_sync_schedules_tenant_scope_target",
        ),
    )
    op.create_index(
        "ix_sync_schedules_tenant_id", "sync_schedules", ["tenant_id"]
    )
    op.create_index(
        "ix_sync_schedules_target_id", "sync_schedules", ["target_id"]
    )
    op.create_index(
        "ix_sync_schedules_next_run_at", "sync_schedules", ["next_run_at"]
    )

    # ═══════════════════════════════════════════════════════════════════
    # Backfill — existing active configurations get the default schedule.
    # Idempotent (ON CONFLICT DO NOTHING); next_run_at points strictly
    # into the future so no run fires on migration day.
    # ═══════════════════════════════════════════════════════════════════
    provider_list = ", ".join(f"'{p}'" for p in _INGESTION_PROVIDERS)
    op.execute(
        sa.text(
            _insert_default_schedule_sql(
                scope="ingestion",
                target_expr="t.target_id",
                source_sql=(
                    "SELECT tenant_id, id::text AS target_id "
                    "FROM credentials "
                    "WHERE status = 'active' "
                    f"AND provider_key IN ({provider_list})"
                ),
            )
        )
    )

    # Export targets are globally env-configured; a tenant counts as
    # having a configured target when it has per-tenant export state.
    exporter_list = ", ".join(f"'{e}'" for e in _EXPORT_EXPORTERS)
    op.execute(
        sa.text(
            _insert_default_schedule_sql(
                scope="export",
                target_expr="t.target_id",
                source_sql=(
                    "SELECT tenant_id, exporter AS target_id FROM ("
                    "  SELECT DISTINCT tenant_id, 'wealthfolio' AS exporter "
                    "  FROM wealthfolio_deliveries "
                    "  UNION "
                    "  SELECT DISTINCT tenant_id, 'wealthfolio' "
                    "  FROM wealthfolio_account_mappings "
                    "  UNION "
                    "  SELECT DISTINCT tenant_id, 'actual-budget' "
                    "  FROM export_deliveries "
                    "  UNION "
                    "  SELECT DISTINCT tenant_id, 'actual-budget' "
                    "  FROM ab_account_mappings "
                    f") AS export_evidence "
                    f"WHERE exporter IN ({exporter_list})"
                ),
            )
        )
    )


def downgrade() -> None:
    op.drop_index("ix_sync_schedules_next_run_at", table_name="sync_schedules")
    op.drop_index("ix_sync_schedules_target_id", table_name="sync_schedules")
    op.drop_index("ix_sync_schedules_tenant_id", table_name="sync_schedules")
    op.drop_table("sync_schedules")
