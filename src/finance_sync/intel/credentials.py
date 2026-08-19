"""Envelope-encrypted credential storage for market-intelligence providers.

Provider secrets (API keys, tokens) for the intel source layer are
persisted with the **existing** envelope-encryption mechanism
(:func:`finance_sync.services.auth.encrypt_credential` /
``decrypt_credential``, AES-256-GCM keyed by ``MASTER_ENCRYPTION_KEY``).
Plaintext secrets are never stored, never logged and never returned by
any read surface: the store only ever hands back decrypted values to
callers that explicitly request them at use time, and every error
message is sanitised before persistence.

The store reuses the generic ``credentials`` table (one row per
connection) with ``provider_key`` namespaced under ``intel:<provider>``
so intel provider credentials live in the same encrypted table as
connector credentials but can never collide with them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.models.credential import Credential
from finance_sync.utils.redaction import sanitize_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.config.settings import Settings
    from finance_sync.db.uow import UnitOfWork

logger = structlog.get_logger(__name__)

#: Namespace prefix for intel provider credentials in the credentials table.
INTEL_CREDENTIAL_PREFIX = "intel:"


def intel_provider_credential_key(provider_key: str) -> str:
    """Return the credentials-table ``provider_key`` for an intel provider."""
    return f"{INTEL_CREDENTIAL_PREFIX}{provider_key}"


def _settings_from_session(uow: UnitOfWork) -> Settings | None:
    """Return the settings attached to the session (or None)."""
    return uow.session.info.get("settings")  # type: ignore[no-any-return]


class IntelCredentialStore:
    """Envelope-encrypted credential store for intel providers.

    One credential row per (tenant, intel provider).  ``save`` encrypts
    the payload before persisting; ``get`` decrypts on demand; errors
    are sanitised before they are stored on the row.
    """

    def __init__(
        self,
        session_or_uow: UnitOfWork | AsyncSession,
        settings: Settings | None = None,
    ) -> None:
        """Wrap a session or a UnitOfWork.

        ``session_or_uow`` may be a :class:`UnitOfWork` (scheduler /
        worker path) or a raw :class:`AsyncSession` (API path).  When a
        raw session is passed, the store manages its own settings from
        the constructor argument.
        """
        from finance_sync.db.uow import UnitOfWork as _UoW

        if isinstance(session_or_uow, _UoW):
            self._uow = session_or_uow
        else:
            self._uow = _UoW(session_or_uow)  # type: ignore[arg-type]
        self._settings = settings

    def _settings_or_error(self) -> Settings:
        settings = self._settings or _settings_from_session(self._uow)
        if settings is None:
            msg = (
                "MASTER_ENCRYPTION_KEY is not configured — intel provider "
                "credential encryption is unavailable"
            )
            raise RuntimeError(msg)
        return settings

    async def save(
        self,
        tenant_id: str,
        provider_key: str,
        credentials: Mapping[str, str],
        *,
        owner_user_id: str | None = None,
    ) -> Credential:
        """Encrypt and persist *credentials* for an intel provider.

        Returns the credential row.  Only non-empty values are stored;
        empty values are treated as "unchanged" (the caller can pass a
        partial dict to rotate a single key without wiping the others).
        """
        from finance_sync.services.auth import encrypt_credential

        settings = self._settings_or_error()
        table_key = intel_provider_credential_key(provider_key)

        existing = await self._find(tenant_id, table_key)
        if existing is not None:
            # Merge with the existing payload so a partial update does
            # not clobber untouched keys.
            merged: dict[str, str] = {}
            try:
                from finance_sync.services.auth import decrypt_credential

                plaintext = decrypt_credential(
                    existing.encrypted_payload, existing.nonce, settings
                )
                parsed: dict[str, Any] = json.loads(plaintext)
                merged = {str(k): str(v) for k, v in parsed.items() if v}
            except Exception:
                # Existing payload is unreadable — start fresh.
                logger.warning(
                    "intel_credential_existing_unreadable",
                    provider_key=provider_key,
                    tenant_id=tenant_id,
                )
            merged.update({k: str(v) for k, v in credentials.items() if v})
            if not merged:
                return existing
            plaintext = json.dumps(merged, separators=(",", ":"))
            ciphertext, nonce = encrypt_credential(plaintext, settings)
            existing.encrypted_payload = ciphertext
            existing.nonce = nonce
            if owner_user_id:
                existing.owner_user_id = owner_user_id
            existing.description = _describe_credentials(merged)
            existing.last_error = None
            existing.last_attempt_at = datetime.now(UTC)
            await self._uow.session.flush()
            return existing

        filtered = {k: str(v) for k, v in credentials.items() if v}
        if not filtered:
            msg = (
                f"no credential values provided for intel provider "
                f"{provider_key!r}"
            )
            raise ValueError(msg)
        plaintext = json.dumps(filtered, separators=(",", ":"))
        ciphertext, nonce = encrypt_credential(plaintext, settings)
        row = Credential(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            provider_key=table_key,
            encrypted_payload=ciphertext,
            nonce=nonce,
            description=_describe_credentials(filtered),
            status="active",
            last_attempt_at=datetime.now(UTC),
        )
        self._uow.session.add(row)
        await self._uow.session.flush()
        return row

    async def get(
        self,
        tenant_id: str,
        provider_key: str,
    ) -> dict[str, str]:
        """Return the decrypted credentials for an intel provider.

        Empty dict when the provider has no stored credentials.  The
        plaintext values exist only in the returned dict — never logged,
        never persisted.
        """
        settings = self._settings_or_error()
        table_key = intel_provider_credential_key(provider_key)
        existing = await self._find(tenant_id, table_key)
        if existing is None or not existing.encrypted_payload:
            return {}
        from finance_sync.services.auth import decrypt_credential

        try:
            plaintext = decrypt_credential(
                existing.encrypted_payload, existing.nonce, settings
            )
            parsed: dict[str, Any] = json.loads(plaintext)
            return {str(k): str(v) for k, v in parsed.items() if v}
        except Exception as exc:
            logger.error(
                "intel_credential_decrypt_failed",
                provider_key=provider_key,
                tenant_id=tenant_id,
                error=type(exc).__name__,
            )
            return {}

    async def has(self, tenant_id: str, provider_key: str) -> bool:
        """True when the tenant has stored credentials for *provider_key*."""
        table_key = intel_provider_credential_key(provider_key)
        existing = await self._find(tenant_id, table_key)
        return existing is not None and bool(existing.encrypted_payload)

    async def status(
        self,
        tenant_id: str,
        provider_key: str,
    ) -> dict[str, Any]:
        """Return a non-secret status snapshot of the stored credentials.

        Includes the *names* of the configured keys (never values) and
        the sanitised last error, so operators and read surfaces can
        show configuration state without exposing secrets.
        """
        table_key = intel_provider_credential_key(provider_key)
        existing = await self._find(tenant_id, table_key)
        if existing is None:
            return {
                "is_configured": False,
                "credential_keys": [],
                "last_error": None,
            }
        keys: list[str] = []
        if existing.description:
            try:
                parsed: dict[str, Any] = json.loads(existing.description)
                raw_keys: list[Any] = parsed.get("intel_credential_keys") or []
                keys = [str(k) for k in raw_keys if k]
            except (json.JSONDecodeError, TypeError):
                keys = []
        return {
            "is_configured": bool(existing.encrypted_payload),
            "credential_keys": keys,
            "last_error": existing.last_error,
        }

    async def delete(self, tenant_id: str, provider_key: str) -> bool:
        """Delete stored credentials for an intel provider.

        Returns True when a row was removed.
        """
        table_key = intel_provider_credential_key(provider_key)
        existing = await self._find(tenant_id, table_key)
        if existing is None:
            return False
        await self._uow.session.delete(existing)
        await self._uow.session.flush()
        return True

    async def record_error(
        self,
        tenant_id: str,
        provider_key: str,
        error: BaseException | str,
    ) -> None:
        """Persist a sanitised error on the credential row (never a secret)."""
        table_key = intel_provider_credential_key(provider_key)
        existing = await self._find(tenant_id, table_key)
        if existing is None:
            return
        existing.last_error = sanitize_error(
            str(error),
            _decrypted_secrets(existing, self._settings_or_error()),
        )
        existing.last_attempt_at = datetime.now(UTC)
        await self._uow.session.flush()

    async def _find(self, tenant_id: str, table_key: str) -> Credential | None:
        """Return the credential row for (tenant, intel provider key)."""
        from sqlalchemy import select

        stmt = select(Credential).where(
            Credential.tenant_id == tenant_id,
            Credential.provider_key == table_key,
        )
        result = await self._uow.session.execute(stmt)
        return result.scalars().first()


def _decrypted_secrets(row: Credential, settings: Any) -> list[str]:
    """Best-effort decryption of a row's secrets for redaction.

    Used only to feed :func:`redact_text` — failures are harmless
    (redaction then relies on the generic secret-shape regexes).
    """
    if not row.encrypted_payload or settings is None:
        return []
    try:
        from finance_sync.services.auth import decrypt_credential

        plaintext = decrypt_credential(
            row.encrypted_payload, row.nonce, settings
        )
        parsed: dict[str, Any] = json.loads(plaintext)
        return [str(v) for v in parsed.values() if v]
    except Exception:
        return []


def _describe_credentials(credentials: Mapping[str, str]) -> str:
    """Build a non-secret description for a credential row.

    Records only the *names* of the stored keys — never their values —
    so operators can see which credentials exist without seeing them.
    """
    return json.dumps(
        {"intel_credential_keys": sorted(credentials.keys())},
        separators=(",", ":"),
    )
