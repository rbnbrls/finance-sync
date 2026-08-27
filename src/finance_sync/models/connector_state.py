"""Per-connector runtime state persistence model.

Stores opaque connector state per ``(tenant, provider)`` — for example the
bunq installation material (client RSA keypair + installation token) that
must survive worker restarts so a connector reuses the same device identity
instead of registering a new one on every sync tick.

The payload is treated as an opaque JSON blob by the framework; individual
connectors decide what to store and how to interpret it.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class ConnectorState(Base):
    """Persistent runtime state for one ``(tenant, provider_key)`` pair.

    One row per connector type per tenant.  The ``state`` JSONB blob is
    written by the sync orchestrator after a sync run when the connector
    exposes new state (see ``SyncOrchestrator``), and injected back into
    the connector before the next run.
    """

    __tablename__ = "connector_state"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider_key",
            "connection_id",
            name="uq_connector_state_tenant_provider",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )
    provider_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Connector name, e.g. 'bunq'",
    )
    connection_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment=(
            "Stable connection (credential) id this state belongs to; "
            "each connection keeps its own installation material"
        ),
    )
    state: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
        comment=(
            "Opaque connector runtime state, e.g. the bunq installation "
            "material (client keypair + installation token)"
        ),
    )

    created_at = created_at_ts()
    updated_at = updated_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ConnectorState tenant={self.tenant_id!r} "
            f"provider={self.provider_key!r} keys={sorted(self.state)}>"
        )
