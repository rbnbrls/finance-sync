"""Intel provider credential management API.

Provider secrets for the market-intelligence source layer (e.g. an
OpenBB API key) are stored with the **existing** envelope-encryption
mechanism (AES-256-GCM keyed by ``MASTER_ENCRYPTION_KEY``).  This
surface is the only place operators configure them; it never returns
the stored values — only whether a key is configured.

Secrets are encrypted before persistence, decrypted only at provider
run time (in the worker), and never appear in responses, logs or
metrics.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container
from finance_sync.intel.credentials import IntelCredentialStore

router = APIRouter(
    prefix="/market-intelligence/credentials",
    tags=["market-intelligence"],
)


class IntelCredentialStatusResponse(BaseModel):
    """Whether an intel provider has credentials configured."""

    provider_key: str = Field(description="Intel provider key, e.g. 'openbb'")
    is_configured: bool = Field(
        description="True when the tenant has stored credentials"
    )
    credential_keys: list[str] = Field(
        default_factory=list,
        description="Names of the stored credential keys (never values)",
    )
    last_error: str | None = Field(
        default=None,
        description="Sanitised last error (secrets redacted)",
    )


class IntelCredentialSetRequest(BaseModel):
    """Payload for storing intel provider credentials (encrypted)."""

    credentials: dict[str, str] = Field(
        description=(
            "Provider secrets, e.g. {'api_key': '...'}.  Empty values "
            "are ignored (partial updates merge with existing keys)."
        ),
    )


def _status_to_response(
    provider_key: str,
    status_data: dict[str, Any],
) -> IntelCredentialStatusResponse:
    """Build a status response from the store's non-secret snapshot."""
    raw_keys: Any = status_data.get("credential_keys") or []
    return IntelCredentialStatusResponse(
        provider_key=provider_key,
        is_configured=bool(status_data.get("is_configured")),
        credential_keys=[str(k) for k in raw_keys],
        last_error=status_data.get("last_error"),
    )


@router.get(
    "/{provider_key}",
    response_model=IntelCredentialStatusResponse,
)
async def get_intel_credential_status(
    request: Request,
    provider_key: str,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
) -> IntelCredentialStatusResponse:
    """Return whether *provider_key* has credentials configured.

    Never returns the stored secret values — only the key names and a
    sanitised last error.
    """
    container = get_container(request)
    session = container.session_factory()
    try:
        store = IntelCredentialStore(session, container.settings)
        status_data = await store.status(auth.tenant_id, provider_key)
        return _status_to_response(provider_key, status_data)
    finally:
        await session.aclose()


@router.put(
    "/{provider_key}",
    response_model=IntelCredentialStatusResponse,
)
async def set_intel_credential(
    request: Request,
    provider_key: str,
    body: IntelCredentialSetRequest,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> IntelCredentialStatusResponse:
    """Store (encrypt) credentials for an intel provider.

    The payload is envelope-encrypted before persistence.  Partial
    updates merge with existing keys; empty values are ignored.  The
    response never contains the secret values.
    """
    container = get_container(request)
    session = container.session_factory()
    try:
        store = IntelCredentialStore(session, container.settings)
        await store.save(
            auth.tenant_id,
            provider_key,
            body.credentials,
            owner_user_id=auth.principal_id,
        )
        await session.commit()
        status_data = await store.status(auth.tenant_id, provider_key)
        return _status_to_response(provider_key, status_data)
    except (ValueError, RuntimeError) as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    finally:
        await session.aclose()


@router.delete("/{provider_key}", response_model=dict[str, Any])
async def delete_intel_credential(
    request: Request,
    provider_key: str,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> dict[str, Any]:
    """Delete stored credentials for an intel provider."""
    container = get_container(request)
    session = container.session_factory()
    try:
        store = IntelCredentialStore(session, container.settings)
        deleted = await store.delete(auth.tenant_id, provider_key)
        await session.commit()
        return {"provider_key": provider_key, "deleted": deleted}
    finally:
        await session.aclose()
