"""Household sharing — account ownership, visibility and invitations.

Implements the "gedeeld huishouden met selectieve accountdeling" story:
tenants gain a two-member household model where every financial account
has an owner and an explicit visibility policy (``private`` /
``household``), and only ``household`` accounts flow into the shared
Wealthfolio export.

Schema changes
--------------
- ``accounts``: add ``owner_user_id`` (nullable, plain string) and
  ``visibility`` (NOT NULL, server default ``'private'``).
- ``credentials``: add ``owner_user_id`` (nullable, plain string) so the
  provenance chain user → connection → account can be established.
- New ``household_invitations``: tenant-scoped, single-use, expiring
  invitations (email, SHA-256-hashed token, role, status, expiry).
- New ``household_audit_log``: tenant-scoped security audit trail for
  household actions (sanitised payloads only).

Backward compatibility / safe default
-------------------------------------
Existing accounts and credentials are backfilled with the tenant's
**oldest admin user** as owner (falling back to the oldest user when the
tenant has no admin).  ``visibility`` defaults to ``'private'`` for all
existing rows — the documented safe default.  The migration therefore
never widens who can see an account: in single-user tenants (the common
self-hosted case) the owner keeps seeing everything, and in multi-user
tenants only the owning admin sees the pre-existing accounts until they
are explicitly shared.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-17
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: SQL that picks the tenant's oldest admin user id (or oldest user when
#: the tenant has no admin).  Uses a lateral-less correlated subquery so
#: it works on both PostgreSQL 13+ and the CI container image.
_OWNER_BACKFILL_SQL = """
UPDATE {table} AS t
SET owner_user_id = (
    SELECT u.id::text
    FROM users AS u
    WHERE u.tenant_id = t.tenant_id
    ORDER BY CASE WHEN u.role = 'admin' THEN 0 ELSE 1 END, u.created_at
    LIMIT 1
)
WHERE t.owner_user_id IS NULL
"""


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════
    # 1. accounts — owner + visibility (private-by-default)
    # ═══════════════════════════════════════════════════════════════════
    op.add_column(
        "accounts",
        sa.Column(
            "owner_user_id",
            sa.String(length=64),
            nullable=True,
            comment=(
                "User id that owns this account (plain string, no FK); "
                "NULL = system-owned/legacy"
            ),
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "visibility",
            sa.String(length=16),
            server_default="private",
            nullable=False,
            comment="'private' or 'household' — see AccountVisibility",
        ),
    )
    op.create_index("ix_accounts_owner_user_id", "accounts", ["owner_user_id"])

    # ═══════════════════════════════════════════════════════════════════
    # 2. credentials — owner (provenance chain)
    # ═══════════════════════════════════════════════════════════════════
    op.add_column(
        "credentials",
        sa.Column(
            "owner_user_id",
            sa.String(length=64),
            nullable=True,
            comment=(
                "User id that configured this connection; NULL = legacy/"
                "system-owned"
            ),
        ),
    )
    op.create_index(
        "ix_credentials_owner_user_id", "credentials", ["owner_user_id"]
    )

    # ═══════════════════════════════════════════════════════════════════
    # 3. Backfill owners — oldest admin (or oldest user) per tenant.
    #    Visibility stays 'private' (the documented safe default): the
    #    migration never widens who can see an account.
    # ═══════════════════════════════════════════════════════════════════
    op.execute(sa.text(_OWNER_BACKFILL_SQL.format(table="accounts")))
    op.execute(sa.text(_OWNER_BACKFILL_SQL.format(table="credentials")))

    # ═══════════════════════════════════════════════════════════════════
    # 4. household_invitations
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "household_invitations",
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
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(length=128),
            nullable=False,
            comment="SHA-256 hex digest of the single-use invite token",
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("accepted_by", sa.String(length=64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_household_invitations_tenant_status",
        "household_invitations",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_household_invitations_token_hash",
        "household_invitations",
        ["token_hash"],
    )
    op.create_index(
        "ix_household_invitations_tenant_id",
        "household_invitations",
        ["tenant_id"],
    )

    # ═══════════════════════════════════════════════════════════════════
    # 5. household_audit_log — tenant-scoped security audit trail
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "household_audit_log",
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
            "action",
            sa.String(length=48),
            nullable=False,
            comment=(
                "invite/revoke_invitation/accept_invitation/role_change/"
                "remove_member/account_share/account_unshare/account_claim/"
                "account_export_quarantine"
            ),
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Sanitised event payload; never contains financial data "
                "or secrets."
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
        "ix_household_audit_tenant_created",
        "household_audit_log",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_household_audit_log_tenant_id",
        "household_audit_log",
        ["tenant_id"],
    )


def downgrade() -> None:
    # Reverse: drop audit log, invitations, and the new columns.
    op.drop_index(
        "ix_household_audit_log_tenant_id", table_name="household_audit_log"
    )
    op.drop_index(
        "ix_household_audit_tenant_created", table_name="household_audit_log"
    )
    op.drop_table("household_audit_log")

    op.drop_index(
        "ix_household_invitations_tenant_id",
        table_name="household_invitations",
    )
    op.drop_index(
        "ix_household_invitations_token_hash",
        table_name="household_invitations",
    )
    op.drop_index(
        "ix_household_invitations_tenant_status",
        table_name="household_invitations",
    )
    op.drop_table("household_invitations")

    op.drop_index("ix_credentials_owner_user_id", table_name="credentials")
    op.drop_column("credentials", "owner_user_id")

    op.drop_index("ix_accounts_owner_user_id", table_name="accounts")
    op.drop_column("accounts", "visibility")
    op.drop_column("accounts", "owner_user_id")
