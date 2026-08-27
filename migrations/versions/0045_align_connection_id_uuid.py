"""Align connection_id columns to UUID (root-cause fix for uuid = varchar).

Migration 0017 added ``connection_id`` as ``varchar(64)`` to the
connection-scoped tables and backfilled them from ``credentials.id::text``.
``credentials.id`` is a native ``uuid`` (``pk_uuid()``), so any JOIN that
compares ``credentials.id = <table>.connection_id`` raises

    operator does not exist: uuid = character varying

in PostgreSQL (asyncpg ``UndefinedFunctionError``) — the exact failure
reported for ``GET /api/v1/sync-runs`` (GlitchTip #4 / issue #451).

This migration converts every varchar ``connection_id`` to native ``uuid``
(``USING connection_id::uuid``), matching the type of ``credentials.id``
and ``import_runs.connection_id``.  All values were backfilled from a uuid
cast to text, so every non-NULL value is a valid UUID; the upgrade still
validates the column content first so a schema with garbage data fails
loudly instead of corrupting rows.

Tables converted (same root cause, all join credentials.id today or will
in future joins):

- sync_runs            (failing join today — issue #451)
- accounts             (unique constraint includes connection_id)
- transactions         (unique constraint includes connection_id)
- card_transactions    (unique constraint includes connection_id)
- scheduled_payments   (unique constraint includes connection_id)
- sync_cursor          (unique constraint includes connection_id)
- connector_state      (unique constraint includes connection_id)
- connection_audit_log

PostgreSQL rebuilds the btree indexes on the converted columns
automatically; the multi-column unique constraints carry over unchanged
because the values are all valid UUIDs.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-27
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables whose ``connection_id`` column must be aligned to uuid.  The
#: column is nullable on every one of them (legacy pre-0017 rows keep
#: NULL; sync_runs was intentionally never backfilled).
_CONNECTION_ID_TABLES: tuple[str, ...] = (
    "sync_runs",
    "accounts",
    "transactions",
    "card_transactions",
    "scheduled_payments",
    "sync_cursor",
    "connector_state",
    "connection_audit_log",
)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_connection_ids(conn: Connection) -> None:
    """Fail loudly if any non-NULL connection_id is not a valid UUID.

    All values originate from ``credentials.id::text`` backfills, so in a
    healthy schema this is a no-op scan.  Running it before the ALTER
    guarantees the ``USING connection_id::uuid`` cast cannot fail halfway
    through a table.
    """
    for table in _CONNECTION_ID_TABLES:
        # information_schema is the reliable way to detect the column type
        # without assuming a prior migration state.
        row = conn.execute(
            sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = 'connection_id'"
            ),
            {"t": table},
        ).first()
        if row is None:
            continue  # column absent — nothing to convert for this table
        data_type = row[0]
        if data_type == "uuid":
            continue  # already converted (idempotent re-run)
        if data_type not in ("character varying", "text"):
            msg = f"connection_id on {table} has unexpected type {data_type!r}"
            raise RuntimeError(msg)

        rows = conn.execute(
            sa.text(
                f"SELECT connection_id FROM {table} "
                "WHERE connection_id IS NOT NULL"
            )
        )
        for (value,) in rows:
            if not _UUID_RE.match(str(value)):
                msg = (
                    f"connection_id value {value!r} on {table} is not a "
                    "valid UUID; cannot convert column to uuid"
                )
                raise RuntimeError(msg)


def upgrade() -> None:
    bind = op.get_bind()
    _validate_connection_ids(bind)

    for table in _CONNECTION_ID_TABLES:
        # Guard against running on a schema where the column is absent.
        row = bind.execute(
            sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = 'connection_id'"
            ),
            {"t": table},
        ).first()
        if row is None:
            continue
        if row[0] == "uuid":
            continue
        op.alter_column(
            table,
            "connection_id",
            existing_type=sa.String(length=64),
            type_=sa.dialects.postgresql.UUID(as_uuid=True),
            existing_nullable=True,
            postgresql_using="connection_id::uuid",
            comment=(
                "Stable connection (credential) id this row belongs to; "
                "uuid matching credentials.id"
            ),
        )


def downgrade() -> None:
    """Reverse the type change (uuid -> varchar(64) via ::text).

    Reversible because uuid values always render as canonical lowercase
    text.  Indexes/constraints are rebuilt automatically by PostgreSQL.
    """
    for table in _CONNECTION_ID_TABLES:
        bind = op.get_bind()
        row = bind.execute(
            sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = 'connection_id'"
            ),
            {"t": table},
        ).first()
        if row is None or row[0] != "uuid":
            continue
        op.alter_column(
            table,
            "connection_id",
            existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
            type_=sa.String(length=64),
            existing_nullable=True,
            postgresql_using="connection_id::text",
        )
