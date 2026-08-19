"""Add holding-relevance feed tables.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-19

Implements backlog/plus-relevant-nieuws-en-events.md: links market-
intelligence observations to current/recently sold holdings, clusters
syndicated stories, tracks per-user acknowledgements and false-positive
corrections, and records opt-in notification state.

Tables:

* ``holding_relevance_items`` — one intel observation matched to one
  held security (match reason, confidence, holding status/weight,
  normalised event date).
* ``relevance_clusters`` — one deduplicated story per
  ``(tenant, story_key)`` with event type/date, source count, best
  source URL and deterministic ranking score.
* ``relevance_cluster_items`` — membership edges (a cluster keeps every
  source item + link).
* ``relevance_acks`` — per-user per-cluster acknowledgement (idempotent).
* ``relevance_corrections`` — per-user false-positive corrections
  (suppression only, never deletes the underlying observation).
* ``relevance_notification_preferences`` — opt-in settings (off by
  default, lockscreen-safe by default).
* ``relevance_notification_log`` — deduplication log per (user,
  cluster, event type).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _timestamps() -> list[sa.Column[Any]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "holding_relevance_items",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_intelligence_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("securities.id", ondelete="CASCADE"),
            nullable=True,
            comment=(
                "Canonical security that matched the item (NULL for "
                "cash/currency interest events)"
            ),
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "match_reason",
            sa.String(length=64),
            nullable=False,
            comment=(
                "canonical_security/recently_sold/currency_interest/"
                "hermes_suggested"
            ),
        ),
        sa.Column(
            "confidence", sa.Float(), nullable=False, server_default="1.0"
        ),
        sa.Column(
            "holding_status",
            sa.String(length=32),
            nullable=False,
            server_default="current",
        ),
        sa.Column("holding_weight", sa.Float(), nullable=True),
        sa.Column(
            "event_date",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Normalised event date (fact-driven, else published date)",
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_holding_relevance_items_tenant_id",
        "holding_relevance_items",
        ["tenant_id"],
    )
    op.create_index(
        "ix_holding_relevance_items_item_id",
        "holding_relevance_items",
        ["item_id"],
    )
    op.create_index(
        "ix_holding_relevance_items_security_id",
        "holding_relevance_items",
        ["security_id"],
    )
    op.create_index(
        "ix_holding_relevance_items_account_id",
        "holding_relevance_items",
        ["account_id"],
    )
    op.create_index(
        "ix_holding_relevance_items_event_date",
        "holding_relevance_items",
        ["event_date"],
    )
    op.create_unique_constraint(
        "uq_holding_relevance_item_security",
        "holding_relevance_items",
        ["tenant_id", "item_id", "security_id"],
    )

    op.create_table(
        "relevance_clusters",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("story_key", sa.String(length=512), nullable=False),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("securities.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("best_source_url", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0.0"),
        *_timestamps(),
    )
    op.create_index(
        "ix_relevance_clusters_tenant_id", "relevance_clusters", ["tenant_id"]
    )
    op.create_index(
        "ix_relevance_clusters_security_id",
        "relevance_clusters",
        ["security_id"],
    )
    op.create_index(
        "ix_relevance_clusters_event_type", "relevance_clusters", ["event_type"]
    )
    op.create_index(
        "ix_relevance_clusters_event_date", "relevance_clusters", ["event_date"]
    )
    op.create_unique_constraint(
        "uq_relevance_cluster_story",
        "relevance_clusters",
        ["tenant_id", "story_key"],
    )

    op.create_table(
        "relevance_cluster_items",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("relevance_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_intelligence_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_index(
        "ix_relevance_cluster_items_tenant_id",
        "relevance_cluster_items",
        ["tenant_id"],
    )
    op.create_index(
        "ix_relevance_cluster_items_cluster_id",
        "relevance_cluster_items",
        ["cluster_id"],
    )
    op.create_index(
        "ix_relevance_cluster_items_item_id",
        "relevance_cluster_items",
        ["item_id"],
    )
    op.create_unique_constraint(
        "uq_relevance_cluster_item",
        "relevance_cluster_items",
        ["cluster_id", "item_id"],
    )

    op.create_table(
        "relevance_acks",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("relevance_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_relevance_acks_tenant_id", "relevance_acks", ["tenant_id"]
    )
    op.create_index("ix_relevance_acks_user_id", "relevance_acks", ["user_id"])
    op.create_index(
        "ix_relevance_acks_cluster_id", "relevance_acks", ["cluster_id"]
    )
    op.create_unique_constraint(
        "uq_relevance_ack_user_cluster",
        "relevance_acks",
        ["tenant_id", "user_id", "cluster_id"],
    )

    op.create_table(
        "relevance_corrections",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_intelligence_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("securities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "action",
            sa.String(length=32),
            nullable=False,
            server_default="dismiss",
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_relevance_corrections_tenant_id",
        "relevance_corrections",
        ["tenant_id"],
    )
    op.create_index(
        "ix_relevance_corrections_user_id",
        "relevance_corrections",
        ["user_id"],
    )
    op.create_index(
        "ix_relevance_corrections_item_id",
        "relevance_corrections",
        ["item_id"],
    )
    op.create_unique_constraint(
        "uq_relevance_correction_user_item",
        "relevance_corrections",
        ["tenant_id", "user_id", "item_id"],
    )

    op.create_table(
        "relevance_notification_preferences",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Opt-in: notifications are off by default",
        ),
        sa.Column(
            "lockscreen_safe",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="Never leak position size/financial value on the lockscreen",
        ),
        sa.Column("event_types", postgresql.JSONB(), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_relevance_notification_preferences_tenant_id",
        "relevance_notification_preferences",
        ["tenant_id"],
    )
    op.create_index(
        "ix_relevance_notification_preferences_user_id",
        "relevance_notification_preferences",
        ["user_id"],
    )
    op.create_unique_constraint(
        "uq_relevance_notif_pref_user",
        "relevance_notification_preferences",
        ["tenant_id", "user_id"],
    )

    op.create_table(
        "relevance_notification_log",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("relevance_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=True,
            comment="Lockscreen-safe payload snapshot (no position sizes/values)",
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_relevance_notification_log_tenant_id",
        "relevance_notification_log",
        ["tenant_id"],
    )
    op.create_index(
        "ix_relevance_notification_log_user_id",
        "relevance_notification_log",
        ["user_id"],
    )
    op.create_index(
        "ix_relevance_notification_log_cluster_id",
        "relevance_notification_log",
        ["cluster_id"],
    )
    op.create_index(
        "ix_relevance_notification_log_sent_at",
        "relevance_notification_log",
        ["sent_at"],
    )
    op.create_unique_constraint(
        "uq_relevance_notif_log_dedupe",
        "relevance_notification_log",
        ["tenant_id", "user_id", "cluster_id", "event_type"],
    )


def downgrade() -> None:
    op.drop_table("relevance_notification_log")
    op.drop_table("relevance_notification_preferences")
    op.drop_table("relevance_corrections")
    op.drop_table("relevance_acks")
    op.drop_table("relevance_cluster_items")
    op.drop_table("relevance_clusters")
    op.drop_table("holding_relevance_items")
