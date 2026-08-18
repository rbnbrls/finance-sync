"""Governed datamart models for downstream consumer access."""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts

DELIVERY_METHODS = {"pull_api", "webhook", "event_feed", "export"}
HOUSEHOLD_SCOPES = {"explicit", "household"}


class DataMart(Base):
    """A versioned, read-only dataset that may be delivered downstream."""

    __tablename__ = "datamarts"
    __table_args__: ClassVar = (
        UniqueConstraint("tenant_id", "key", name="uq_datamarts_tenant_key"),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="e.g. cash-ledger or portfolio"
    )
    schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="e.g. pfc/1.0"
    )
    fields: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Fields exposed by this mart",
    )
    delivery_method: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="pull_api/webhook/event_feed/export"
    )
    delivery_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Non-secret delivery configuration",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()


class DataMartConsumer(Base):
    """A downstream tool identity, optionally bound to one API key."""

    __tablename__ = "datamart_consumers"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id", "key", name="uq_datamart_consumers_tenant_key"
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()


class DataMartGrant(Base):
    """A consumer's least-privilege grant to a particular datamart."""

    __tablename__ = "datamart_grants"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "consumer_id",
            "datamart_id",
            name="uq_datamart_grants_consumer_mart",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    consumer_id: Mapped[str] = mapped_column(
        ForeignKey("datamart_consumers.id", ondelete="CASCADE"), nullable=False
    )
    datamart_id: Mapped[str] = mapped_column(
        ForeignKey("datamarts.id", ondelete="CASCADE"), nullable=False
    )
    household_scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="explicit",
        comment="explicit/household",
    )
    allowed_account_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment=(
            "Explicit account ids; empty means no accounts for explicit scope"
        ),
    )
    allowed_fields: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Subset of datamart fields; empty means all datamart fields",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()
