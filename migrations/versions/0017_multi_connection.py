"""Multiple connections per provider + connection-scoped data isolation.

Implements the "meerdere instellingsverbindingen en accountselectie"
story: a tenant may now configure several bunq / Trading212 / other
connections, each with its own credentials, label, enabled/paused status
and selected accounts.

Schema changes
--------------
- ``credentials``: add ``status`` (active/paused), ``selected_accounts``
  (JSONB), ``last_attempt_at``, ``last_success_at``, ``last_error``;
  drop the unique index on ``(tenant_id, provider_key)`` so multiple
  rows per provider are allowed.  The row ``id`` doubles as the stable
  ``connection_id``.
- ``accounts`` / ``transactions`` / ``card_transactions`` /
  ``scheduled_payments``: add nullable ``connection_id`` and extend the
  unique constraint to include it, so identical external ids from two
  connections never collide.
- ``sync_cursor``: add nullable ``connection_id`` and extend the unique
  constraint ``(tenant_id, connector, resource)`` to include it.
- ``sync_runs``: add nullable ``connection_id`` (informational scoping).
  Legacy rows keep NULL: ``sync_runs`` carries no ``tenant_id`` column, so
  a per-tenant backfill is impossible and the column is only populated by
  new runs.
- ``connector_state``: add nullable ``connection_id`` and extend the
  unique constraint to include it (per-connection installation state).
- New ``connection_audit_log`` table: tenant-scoped, sanitised audit
  trail for sensitive connection-lifecycle actions.

Backward compatibility
----------------------
All existing rows are backfilled: each row in the scoped tables is
assigned the ``connection_id`` of the tenant's (then-singular) credential
for the matching provider, using each credential row's id.  This only
works because the old unique constraint still guarantees at most one
credential per (tenant, provider) at migration time — the backfill runs
before the constraint is dropped.  Legacy-sourced rows therefore remain
fully traceable to their originating connection.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-17
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Connector keys whose sync runs / cursors map to a different provider
#: credential key.
_CONNECTOR_TO_CREDENTIAL = {
    "bunq_cards": "bunq",
}


def _provider_key_expr(column: str) -> str:
    """SQL expression mapping a connector column to the credential key."""
    mapping = ", ".join(
        f"WHEN {column} = '{k}' THEN '{v}'"
        for k, v in _CONNECTOR_TO_CREDENTIAL.items()
    )
    return f"CASE {mapping} ELSE {column} END"


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════
    # 1. credentials — new connection lifecycle columns
    # ═══════════════════════════════════════════════════════════════════
    op.add_column(
        "credentials",
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="active",
            nullable=False,
            comment="Connection state: 'active' or 'paused'",
        ),
    )
    op.add_column(
        "credentials",
        sa.Column(
            "selected_accounts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Provider account IDs to sync for this connection; NULL/empty "
                "means 'sync all accounts the provider offers'"
            ),
        ),
    )
    op.add_column(
        "credentials",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "credentials",
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "credentials",
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    # Existing rows get a generated user-facing label when they have none
    # (plain provider key).  The description column doubles as the label
    # carrier (JSON ``_label`` key, see the Credential model); rows that
    # already carry a description are left untouched so upgrades never
    # destroy user data.
    op.execute(
        sa.text(
            "UPDATE credentials SET description = provider_key "
            "WHERE description IS NULL OR btrim(description) = ''"
        )
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. Add nullable connection_id to the scoped tables (backfilled
    #    below, before the unique credentials index is dropped).
    # ═══════════════════════════════════════════════════════════════════
    for table in (
        "accounts",
        "transactions",
        "card_transactions",
        "scheduled_payments",
        "sync_cursor",
        "sync_runs",
        "connector_state",
    ):
        op.add_column(
            table,
            sa.Column("connection_id", sa.String(length=64), nullable=True),
        )

    # ═══════════════════════════════════════════════════════════════════
    # 3. Backfill connection_id from the (singular) credential per
    #    (tenant, provider) — safe because the old unique index below
    #    still guarantees at most one credential per provider per tenant.
    # ═══════════════════════════════════════════════════════════════════
    backfills = [
        (
            "accounts",
            "provider_key",
            "UPDATE accounts AS a SET connection_id = c.id::text FROM credentials AS c "
            "WHERE c.tenant_id = a.tenant_id AND c.provider_key = a.provider_key",
        ),
        (
            "transactions",
            "provider_key",
            "UPDATE transactions AS t SET connection_id = c.id::text FROM credentials AS c "
            "WHERE c.tenant_id = t.tenant_id AND c.provider_key = t.provider_key",
        ),
        (
            "card_transactions",
            "provider_key",
            "UPDATE card_transactions AS ct SET connection_id = c.id::text FROM credentials AS c "
            "WHERE c.tenant_id = ct.tenant_id AND c.provider_key = ct.provider_key",
        ),
        (
            "scheduled_payments",
            "provider_key",
            "UPDATE scheduled_payments AS sp SET connection_id = c.id::text FROM credentials AS c "
            "WHERE c.tenant_id = sp.tenant_id AND c.provider_key = sp.provider_key",
        ),
        (
            "sync_cursor",
            "connector",
            "UPDATE sync_cursor AS sc SET connection_id = c.id::text FROM credentials AS c "
            f"WHERE c.tenant_id = sc.tenant_id AND c.provider_key = {_provider_key_expr('sc.connector')}",
        ),
        # sync_runs intentionally not backfilled: the table has no
        # tenant_id column, so legacy rows keep connection_id NULL.
        (
            "connector_state",
            "provider_key",
            "UPDATE connector_state AS cs SET connection_id = c.id::text FROM credentials AS c "
            "WHERE c.tenant_id = cs.tenant_id AND c.provider_key = cs.provider_key",
        ),
    ]
    for _table, _col, sql in backfills:
        op.execute(sa.text(sql))

    # ═══════════════════════════════════════════════════════════════════
    # 4. Drop the unique restriction on (tenant_id, provider_key) so a
    #    tenant can configure multiple connections per provider.
    #    ``if_exists``: migration 0009's upgrade already dropped this
    #    index, so fresh databases do not have it — the drop must not
    #    fail on either schema state.
    # ═══════════════════════════════════════════════════════════════════
    op.drop_index(
        "ix_credentials_tenant_provider",
        table_name="credentials",
        if_exists=True,
    )

    # ═══════════════════════════════════════════════════════════════════
    # 5. Extend the provider-scoped unique constraints with connection_id.
    # ═══════════════════════════════════════════════════════════════════
    _replace_unique(
        "accounts",
        "uq_accounts_provider",
        ["tenant_id", "provider_key", "connection_id", "external_account_id"],
    )
    _replace_unique(
        "transactions",
        "uq_transactions_provider",
        [
            "tenant_id",
            "provider_key",
            "connection_id",
            "external_transaction_id",
        ],
    )
    _replace_unique(
        "card_transactions",
        "uq_card_transactions_provider",
        [
            "tenant_id",
            "provider_key",
            "connection_id",
            "external_card_transaction_id",
        ],
    )
    _replace_unique(
        "scheduled_payments",
        "uq_scheduled_payments_provider",
        ["tenant_id", "provider_key", "connection_id", "external_schedule_id"],
    )
    _replace_unique(
        "sync_cursor",
        "uq_sync_cursor_tenant_connector_resource",
        ["tenant_id", "connector", "connection_id", "resource"],
    )
    _replace_unique(
        "connector_state",
        "uq_connector_state_tenant_provider",
        ["tenant_id", "provider_key", "connection_id"],
    )

    # ═══════════════════════════════════════════════════════════════════
    # 6. Indexes matching the ORM (index=True on the new columns).
    # ═══════════════════════════════════════════════════════════════════
    for table in (
        "accounts",
        "transactions",
        "card_transactions",
        "scheduled_payments",
    ):
        op.create_index(
            f"ix_{table}_connection_id",
            table,
            ["connection_id"],
        )
    op.create_index(
        "ix_sync_cursor_connection_id", "sync_cursor", ["connection_id"]
    )
    op.create_index(
        "ix_sync_runs_connection_id", "sync_runs", ["connection_id"]
    )
    op.create_index(
        "ix_connector_state_connection_id", "connector_state", ["connection_id"]
    )

    # ═══════════════════════════════════════════════════════════════════
    # 7. connection_audit_log — tenant-scoped security audit trail.
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "connection_audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Connection (credential) id this event refers to; kept as a "
                "plain string so the audit trail survives credential deletion"
            ),
        ),
        sa.Column(
            "provider_key",
            sa.String(length=64),
            nullable=False,
            comment="Connector name, e.g. 'bunq', 'trading212'",
        ),
        sa.Column(
            "action",
            sa.String(length=32),
            nullable=False,
            comment="create/update/test/pause/resume/select_accounts/delete",
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Sanitised event payload; never contains secrets or "
                "financial data."
            ),
        ),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_connection_audit_tenant_created",
        "connection_audit_log",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_connection_audit_connection",
        "connection_audit_log",
        ["connection_id"],
    )
    op.create_index(
        "ix_connection_audit_log_tenant_id",
        "connection_audit_log",
        ["tenant_id"],
    )


def _replace_unique(
    table: str,
    constraint: str,
    columns: list[str],
) -> None:
    """Drop an existing unique constraint and re-create it with new columns."""
    op.drop_constraint(constraint, table, type_="unique")
    op.create_unique_constraint(constraint, table, columns)


def downgrade() -> None:
    # Reverse: drop audit log, drop the new unique constraints + indexes,
    # restore the old unique constraints, drop the new columns, and
    # restore the (tenant_id, provider_key) unique index on credentials.
    op.drop_index(
        "ix_connection_audit_log_tenant_id", table_name="connection_audit_log"
    )
    op.drop_index(
        "ix_connection_audit_connection", table_name="connection_audit_log"
    )
    op.drop_index(
        "ix_connection_audit_tenant_created", table_name="connection_audit_log"
    )
    op.drop_table("connection_audit_log")

    for table in (
        "accounts",
        "transactions",
        "card_transactions",
        "scheduled_payments",
    ):
        op.drop_index(f"ix_{table}_connection_id", table_name=table)
    op.drop_index("ix_sync_cursor_connection_id", table_name="sync_cursor")
    op.drop_index("ix_sync_runs_connection_id", table_name="sync_runs")
    op.drop_index(
        "ix_connector_state_connection_id", table_name="connector_state"
    )

    # Restore provider-scoped unique constraints without connection_id
    _restore_unique(
        "accounts",
        "uq_accounts_provider",
        [
            "tenant_id",
            "provider_key",
            "external_account_id",
        ],
    )
    _restore_unique(
        "transactions",
        "uq_transactions_provider",
        [
            "tenant_id",
            "provider_key",
            "external_transaction_id",
        ],
    )
    _restore_unique(
        "card_transactions",
        "uq_card_transactions_provider",
        [
            "tenant_id",
            "provider_key",
            "external_card_transaction_id",
        ],
    )
    _restore_unique(
        "scheduled_payments",
        "uq_scheduled_payments_provider",
        [
            "tenant_id",
            "provider_key",
            "external_schedule_id",
        ],
    )
    _restore_unique(
        "sync_cursor",
        "uq_sync_cursor_tenant_connector_resource",
        [
            "tenant_id",
            "connector",
            "resource",
        ],
    )
    _restore_unique(
        "connector_state",
        "uq_connector_state_tenant_provider",
        [
            "tenant_id",
            "provider_key",
        ],
    )

    for table in (
        "accounts",
        "transactions",
        "card_transactions",
        "scheduled_payments",
        "sync_cursor",
        "sync_runs",
        "connector_state",
    ):
        op.drop_column(table, "connection_id")

    op.drop_column("credentials", "last_error")
    op.drop_column("credentials", "last_success_at")
    op.drop_column("credentials", "last_attempt_at")
    op.drop_column("credentials", "selected_accounts")
    op.drop_column("credentials", "status")

    # The (tenant_id, provider_key) unique index is restored by
    # migration 0009's downgrade (which runs later in the chain) — do
    # not recreate it here or the downgrade fails with a duplicate.


def _restore_unique(table: str, constraint: str, columns: list[str]) -> None:
    op.drop_constraint(constraint, table, type_="unique")
    op.create_unique_constraint(constraint, table, columns)
