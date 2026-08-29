"""Make uq_transactions_provider NULLS NOT DISTINCT.

The ``transactions`` unique constraint

    uq_transactions_provider (tenant_id, provider_key, connection_id,
    external_transaction_id)

is the conflict target for the sync batch upsert
(``INSERT .. ON CONFLICT DO UPDATE``).  PostgreSQL's default ``UNIQUE``
treats NULLs as *distinct*, so two rows with ``connection_id IS NULL``
never conflict with each other.  A sync run that has no connection scope
(``connection_id = NULL`` — the common single-credential case, and the
integration-test path) therefore inserts a duplicate row on every
re-sync: the ON CONFLICT clause never fires, and the old per-row ORM
upsert masked the problem because its ``get_by_external_id`` lookup
conditionally omitted the NULL connection filter.

Rebuilding the constraint as ``NULLS NOT DISTINCT`` (PostgreSQL 15+)
makes the database treat NULL connection_ids as equal for uniqueness,
so ``ON CONFLICT (tenant_id, provider_key, connection_id,
external_transaction_id)`` fires for NULL-connection rows exactly like
the ORM lookup does.

Safety on existing databases
----------------------------

A database that reached this migration through the pre-0046 chain may
already contain duplicate rows that the old NULLS-DISTINCT constraint
allowed (same provider external id, ``connection_id IS NULL``, inserted
by repeated re-syncs).  Rebuilding the constraint fails on those rows.
Like migration 0013 did for ``holdings``, this migration first removes
such duplicates deterministically — keeping the *oldest* row per
natural key (``created_at``, then id) — before recreating the
constraint.  Fresh databases have no duplicates and the DELETE is a
no-op.

Idempotency
-----------

The upgrade is safe to run on a schema that already has the NULLS NOT
DISTINCT form (e.g. a partial rollout or a re-run): it inspects the
live constraint definition via the catalog and skips the rebuild when
the ``nulls_not_distinct`` flag is already set.  It also tolerates the
constraint being absent entirely (creates it).

The project already targets PostgreSQL 16/17 (docker-compose.test.yml,
CI service containers), so ``NULLS NOT DISTINCT`` is safe.  The ORM
model (``finance_sync.models.transaction.Transaction.__table_args__``)
is updated to match in the same change; the model uses
``sqlalchemy.UniqueConstraint(..., postgresql_nulls_not_distinct=True)``
which requires SQLAlchemy >= 2.0.30 (project pins 2.0.51).

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_transactions_provider"
_TABLE = "transactions"
#: Natural key columns of the constraint, in constraint order.
_KEY_COLUMNS = (
    "tenant_id",
    "provider_key",
    "connection_id",
    "external_transaction_id",
)


def _constraint_nulls_not_distinct(conn: Connection) -> bool:
    """Return True when the constraint already has NULLS NOT DISTINCT.

    Inspects ``pg_constraint.conindid`` -> ``pg_index.indnullsnotdistinct``
    (PostgreSQL 15+), which is the authoritative flag for the index that
    backs a unique constraint.  Returns False when the constraint is
    missing entirely.
    """
    row = conn.execute(
        sa.text(
            "SELECT i.indnullsnotdistinct "
            "FROM pg_constraint c "
            "JOIN pg_index i ON i.indexrelid = c.conindid "
            "WHERE c.conname = :name "
            "AND c.conrelid = 'transactions'::regclass "
            "AND c.contype = 'u'"
        ),
        {"name": _CONSTRAINT},
    ).first()
    return bool(row and row[0])


def _constraint_columns(conn: Connection) -> tuple[str, ...] | None:
    """Return the constraint's current column list, or None if absent."""
    row = conn.execute(
        sa.text(
            "SELECT array_agg(a.attname ORDER BY u.ordinality) "
            "FROM pg_constraint c "
            "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY "
            "AS u(attnum, ordinality) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "AND a.attnum = u.attnum "
            "WHERE c.conname = :name "
            "AND c.conrelid = 'transactions'::regclass "
            "AND c.contype = 'u' "
            "GROUP BY c.oid"
        ),
        {"name": _CONSTRAINT},
    ).first()
    return tuple(row[0]) if row and row[0] else None


def _dedupe_duplicate_natural_keys(conn: Connection) -> None:
    """Remove rows that violate the NULLS NOT DISTINCT natural key.

    Keeps the oldest row per (tenant_id, provider_key, connection_id,
    external_transaction_id) — deterministic and identical to the 0013
    holdings dedupe policy.  No-op when no duplicates exist.
    """
    conn.execute(
        sa.text(
            "DELETE FROM transactions "
            "WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY tenant_id, provider_key, connection_id, "
            "                   external_transaction_id "
            "      ORDER BY created_at, id"
            "    ) AS duplicate_number "
            "    FROM transactions"
            "  ) ranked "
            "  WHERE duplicate_number > 1"
            ")"
        )
    )


def upgrade() -> None:
    """Rebuild uq_transactions_provider as NULLS NOT DISTINCT."""
    bind = op.get_bind()

    # Idempotency: a schema that already has the NULLS NOT DISTINCT form
    # (partial rollout, re-run) is left untouched.
    if _constraint_nulls_not_distinct(bind):
        return

    # Safety: remove any duplicate natural keys the old NULLS-DISTINCT
    # constraint allowed before recreating it (rebuild would fail).
    _dedupe_duplicate_natural_keys(bind)

    if _constraint_columns(bind) is not None:
        bind.execute(
            sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
        )
    bind.execute(
        sa.text(
            f"ALTER TABLE {_TABLE} "
            f"ADD CONSTRAINT {_CONSTRAINT} UNIQUE NULLS NOT DISTINCT "
            "(" + ", ".join(_KEY_COLUMNS) + ")"
        )
    )


def downgrade() -> None:
    """Restore the default NULLS DISTINCT semantics."""
    bind = op.get_bind()
    if not _constraint_nulls_not_distinct(bind):
        return
    bind.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}"))
    bind.execute(
        sa.text(
            f"ALTER TABLE {_TABLE} "
            f"ADD CONSTRAINT {_CONSTRAINT} UNIQUE "
            "(" + ", ".join(_KEY_COLUMNS) + ")"
        )
    )
