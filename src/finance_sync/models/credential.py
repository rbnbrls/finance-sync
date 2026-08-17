"""Encrypted provider credential storage model.

Provider secrets (passwords, tokens, client IDs) are stored as
envelope-encrypted blobs using AES-256-GCM.  The deployment master key
is configured via the ``MASTER_ENCRYPTION_KEY`` setting and is **never**
stored in the database.

A ``Credential`` row is a single **connection**: one credential set for
one provider.  Multiple rows with the same ``provider_key`` within a
tenant are allowed (the historical unique constraint on
``(tenant_id, provider_key)`` was removed in migration 0017), so a
tenant can configure several bunq or Trading212 logins side by side.
Each row additionally carries a user label (stored in the ``description``
JSON as ``_label``), an enabled/paused status, the selected provider
accounts to sync, and the latest sync outcome for the connection UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts

#: Status values for a connection (Credential row).
CONNECTION_STATUS_ACTIVE = "active"
CONNECTION_STATUS_PAUSED = "paused"
CONNECTION_STATUSES = {CONNECTION_STATUS_ACTIVE, CONNECTION_STATUS_PAUSED}


class Credential(Base):
    """Encrypted provider credential for external financial APIs.

    One row = one connection. ``id`` doubles as the stable
    ``connection_id`` referenced by accounts, transactions, sync runs,
    cursors and connector state so data is traceable to the exact
    connection it was fetched with.
    """

    __tablename__ = "credentials"
    __table_args__: ClassVar = {
        "comment": "Envelope-encrypted provider credentials (AES-256-GCM)",
    }

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    # The user that configured this connection. Accounts synced through
    # this connection inherit the owner (provenance chain user →
    # connection → account). Plain string, no FK, so the row survives
    # user deletion.
    owner_user_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment=(
            "User id that configured this connection; NULL = legacy/"
            "system-owned"
        ),
    )
    provider_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Provider identifier, e.g. 'bunq', 'trading212'",
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
        comment=(
            "JSON object with the non-secret connector options; the "
            "user-facing connection label is stored as the ``_label`` "
            "key so it survives credential updates"
        ),
    )
    # ── Connection lifecycle (multi-connection support) ─────────────
    status: Mapped[str] = mapped_column(
        String(16),
        default=CONNECTION_STATUS_ACTIVE,
        nullable=False,
        comment="Connection state: 'active' or 'paused'",
    )
    selected_accounts: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "Provider account IDs to sync for this connection; NULL/empty "
            "means 'sync all accounts the provider offers'"
        ),
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the last sync attempt for this connection started",
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the last successful sync for this connection completed",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Sanitised error of the last failed sync / connection test "
            "(secrets redacted, truncated)"
        ),
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()

    @property
    def connection_id(self) -> str:
        """Stable connection identifier — the credential row's id."""
        return self.id
