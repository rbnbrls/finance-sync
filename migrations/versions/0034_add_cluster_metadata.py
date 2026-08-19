"""Add cluster metadata columns to relevance_clusters.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-19

Implements backlog/plus-relevant-nieuws-en-events.md clustering
acceptance criteria: every cluster exposes *why* it merged
(``cluster_reason``: exact_event / title_duplicate / no_date) and the
earliest published timestamp across its source items
(``earliest_published_at``).

``cluster_reason`` is nullable so pre-existing rows (created before this
migration) keep a NULL reason; the next ``build_feed`` run backfills it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "relevance_clusters",
        sa.Column(
            "cluster_reason",
            sa.String(length=32),
            nullable=True,
            comment=(
                "Why items merged: exact_event (same security+type+event "
                "date), title_duplicate (title fingerprint match), "
                "no_date (fallback)"
            ),
        ),
    )
    op.add_column(
        "relevance_clusters",
        sa.Column(
            "earliest_published_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Earliest published_at across the cluster's source items",
        ),
    )


def downgrade() -> None:
    op.drop_column("relevance_clusters", "earliest_published_at")
    op.drop_column("relevance_clusters", "cluster_reason")
