"""Incremental sync cursor (watermark) persistence model."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class SyncCursor(Base):
    """Persisted watermark for incremental syncs.

    One row per ``(tenant, connector, resource)`` — e.g. one row per
    external account for a connector's transaction sync, and one row
    with resource ``card_transactions`` for the cards pipeline.  The
    orchestrator reads the stored cursor at sync start so a re-run
    resumes from the last successful position instead of always
    re-fetching the full default (90-day) window.  The cursor is
    advanced to the run start timestamp on every successful run and
    never on a failed one — the write is transactional with the
    ``SyncRun`` completion.
    """

    __tablename__ = "sync_cursor"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "connector",
            "connection_id",
            "resource",
            name="uq_sync_cursor_tenant_connector_resource",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )
    connector: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Connector name, e.g. 'bunq', 'bunq_cards'",
    )
    connection_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment=(
            "Stable connection (credential) id this cursor belongs to; "
            "keeps same-provider connections from sharing watermarks"
        ),
    )
    resource: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment=(
            "Sync resource, e.g. an external account id for transaction "
            "sync or 'card_transactions' for the cards pipeline"
        ),
    )
    cursor: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment=(
            "Watermark: the run start time of the last successful sync; "
            "the next run fetches transactions on or after this time"
        ),
    )

    created_at = created_at_ts()
    updated_at = updated_at_ts()

    def __repr__(self) -> str:
        return (
            f"<SyncCursor tenant={self.tenant_id!r} "
            f"connector={self.connector!r} resource={self.resource!r} "
            f"cursor={self.cursor!r}>"
        )
