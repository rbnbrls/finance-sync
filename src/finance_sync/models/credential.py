"""Encrypted provider credential storage model.

Provider secrets (passwords, tokens, client IDs) are stored as
envelope-encrypted blobs using AES-256-GCM.  The deployment master key
is configured via the ``MASTER_ENCRYPTION_KEY`` setting and is **never**
stored in the database.
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import ForeignKey, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class Credential(Base):
    """Encrypted provider credential for external financial APIs."""

    __tablename__ = "credentials"
    __table_args__: ClassVar = {
        "comment": "Envelope-encrypted provider credentials (AES-256-GCM)",
    }

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    provider_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Provider identifier, e.g. 'plaid', 'teller', 'yodlee'",
    )
    encrypted_payload: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="AES-256-GCM ciphertext (includes 16-byte GCM auth tag)",
    )
    nonce: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="12-byte randomly generated nonce / IV",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable label for this credential entry",
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()
