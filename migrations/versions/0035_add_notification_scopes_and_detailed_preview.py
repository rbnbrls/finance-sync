"""Add per-security/account scopes and detailed-preview opt-in to notification preferences.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-19

Implements backlog/plus-relevant-nieuws-en-events.md notification
acceptance criteria: notification settings are opt-in per
tenant/account/security/event type, and detailed lock-screen previews
require an **explicit** opt-in flag.

Changes:

* ``relevance_notification_preferences.security_id`` — optional
  per-security scope (NULL/empty = all).
* ``relevance_notification_preferences.account_id`` — optional
  per-account scope (NULL/empty = all).
* ``relevance_notification_preferences.detailed_preview`` — explicit
  opt-in that adds security ticker/name to the payload.  Off by default:
  lock-screen payloads still never carry position sizes or financial
  values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "relevance_notification_preferences",
        sa.Column(
            "detailed_preview",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "Explicit opt-in: include security ticker/name in the "
                "notification preview (still never financial values)"
            ),
        ),
    )
    op.add_column(
        "relevance_notification_preferences",
        sa.Column(
            "security_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("securities.id", ondelete="CASCADE"),
            nullable=True,
            comment=(
                "Optional per-security scope: only notify for clusters of "
                "this security (NULL/empty = all)"
            ),
        ),
    )
    op.add_column(
        "relevance_notification_preferences",
        sa.Column(
            "account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=True,
            comment=(
                "Optional per-account scope: only notify for clusters "
                "touching this account (NULL/empty = all)"
            ),
        ),
    )
    op.create_index(
        "ix_relevance_notification_preferences_security_id",
        "relevance_notification_preferences",
        ["security_id"],
    )
    op.create_index(
        "ix_relevance_notification_preferences_account_id",
        "relevance_notification_preferences",
        ["account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_relevance_notification_preferences_account_id",
        table_name="relevance_notification_preferences",
    )
    op.drop_index(
        "ix_relevance_notification_preferences_security_id",
        table_name="relevance_notification_preferences",
    )
    op.drop_column("relevance_notification_preferences", "account_id")
    op.drop_column("relevance_notification_preferences", "security_id")
    op.drop_column("relevance_notification_preferences", "detailed_preview")
