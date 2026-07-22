"""Add webhooks and webhook_delivery_logs tables for event notifications.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-21
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════
    # 1. webhooks — registered webhook endpoints
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "webhooks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "url",
            sa.String(2048),
            nullable=False,
            comment="Webhook callback URL",
        ),
        sa.Column(
            "secret",
            sa.String(128),
            nullable=False,
            comment="HMAC-SHA256 signing secret",
        ),
        sa.Column(
            "events",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="List of subscribed event types",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "description",
            sa.String(255),
            nullable=True,
            comment="Optional human-readable label",
        ),
        sa.Column(
            "rate_limit_max_per_minute",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
            comment="Max deliveries per 60-second sliding window",
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_webhooks_tenant_id_tenants",
        ),
        comment="Registered webhook endpoints for event notifications",
    )
    op.create_index(
        "ix_webhooks_tenant_active",
        "webhooks",
        ["tenant_id", "is_active"],
        postgresql_where=sa.text("is_active IS TRUE"),
    )
    op.create_index(
        "ix_webhooks_events_gin",
        "webhooks",
        ["events"],
        postgresql_using="gin",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. webhook_delivery_logs — delivery attempt records
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "webhook_delivery_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "webhook_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
            comment="FK to webhooks.id (audit safety, no actual FK)",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            sa.String(64),
            nullable=False,
            comment="e.g. 'sync.completed'",
        ),
        sa.Column(
            "event_id",
            sa.String(128),
            nullable=True,
            comment="Source event / outbox-message id for tracing",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=True,
            comment="The full JSON payload sent",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="'pending', 'delivered', 'failed', 'rate_limited'",
        ),
        sa.Column(
            "attempt_number",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5"),
        ),
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Next retry time (null if max reached or delivered)",
        ),
        sa.Column(
            "response_status_code",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "response_body",
            sa.Text(),
            nullable=True,
            comment="Truncated response body",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=True,
            comment="Round-trip duration in milliseconds",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        comment="Audit log of webhook delivery attempts",
    )
    op.create_index(
        "ix_webhook_delivery_logs_status_retry",
        "webhook_delivery_logs",
        ["status", "next_retry_at"],
        postgresql_where=sa.text(
            "status = 'failed' AND next_retry_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_table("webhook_delivery_logs")
    op.drop_table("webhooks")
