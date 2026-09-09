"""Add per-connector runtime state persistence (bunq installation).

The bunq connector now performs the full installation flow (RSA keypair →
``/installation`` → ``/device-server`` → signed ``/session-server``) for
fresh API keys.  That flow must run only once per API key: re-running it on
every 15-minute sync tick would register a new device per tick and
eventually exhaust bunq's per-key device limit.  The install material
(private key PEM + installation token) is persisted here so subsequent syncs
reuse the same installation and only create a new signed session.

- Create ``connector_state`` (tenant_id, provider_key, state JSONB,
  created_at, updated_at) with a unique ``(tenant_id, provider_key)``
  constraint — one row per connector per tenant.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the connector_state table."""
    op.create_table(
        "connector_state",
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
            "provider_key",
            sa.String(length=64),
            nullable=False,
            comment="Connector name, e.g. 'bunq'",
        ),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Opaque connector runtime state, e.g. the bunq installation "
                "material (client keypair + installation token)"
            ),
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
        sa.UniqueConstraint(
            "tenant_id",
            "provider_key",
            name="uq_connector_state_tenant_provider",
        ),
    )
    op.create_index(
        "ix_connector_state_tenant_id",
        "connector_state",
        ["tenant_id"],
    )


def downgrade() -> None:
    """Drop the connector_state table."""
    op.drop_index("ix_connector_state_tenant_id", table_name="connector_state")
    op.drop_table("connector_state")
