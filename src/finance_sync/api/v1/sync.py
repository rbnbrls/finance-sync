"""Sync trigger endpoints — ``POST /sync`` and ``POST /sync/{provider}``.

Starts connector syncs for the authenticated tenant, reusing the same
credential-decrypt → ``SyncOrchestrator`` pipeline as the MCP
``run_sync`` tool (``mcp/server.py``).  Returns 202 with per-provider
sync-run links and the collection ``meta`` envelope.

``resources`` and ``force`` are accepted for forward compatibility with
``docs/API.md``; the orchestrator currently syncs all resources per
provider on every run, so they are validated but have no effect.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.credential import Credential
from finance_sync.models.sync_run import SyncRun
from finance_sync.schemas.freshness import CollectionMeta
from finance_sync.services.auth import decrypt_credential
from finance_sync.sync.orchestrator import SyncOrchestrator

router = APIRouter(prefix="/sync", tags=["sync"])


# ── Request / response models ─────────────────────────────────────────


class SyncTriggerRequest(BaseModel):
    """Body for ``POST /sync`` — ``{providers?, resources?, force?}``."""

    providers: list[str] | None = Field(
        default=None,
        description=(
            "Connector/provider keys to sync (e.g. ['bunq']); defaults "
            "to all configured connectors for the tenant"
        ),
    )
    resources: list[str] | None = Field(
        default=None,
        description=(
            "Resource subset to sync (reserved; the orchestrator syncs "
            "all resources per provider)"
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "Force a full re-sync (reserved; run_sync already re-fetches "
            "its default look-back window)"
        ),
    )


class SyncRunLink(BaseModel):
    """Per-provider sync outcome with a link to the sync run."""

    provider: str
    sync_run_id: str | None = None
    status: str
    accounts_synced: int = 0
    transactions_synced: int = 0
    error_message: str | None = None
    link: str | None = None


class SyncTriggerResponse(BaseModel):
    """202 response for sync triggers — sync-run links + ``meta``."""

    sync_runs: list[SyncRunLink]
    meta: CollectionMeta = Field(
        default_factory=CollectionMeta,
        description=(
            "As-of / currency / cursor / freshness envelope "
            "(docs/API.md ``meta`` contract)"
        ),
    )


# ── Helpers ───────────────────────────────────────────────────────────


async def _latest_run_id(db: AsyncSession, provider: str) -> str | None:
    """Return the most recent sync-run id for a connector.

    ``SyncRun.id`` is a UUID column; on PostgreSQL the ORM returns a
    ``uuid.UUID`` object, which pydantic refuses to coerce into the
    ``str`` fields of ``SyncRunLink`` — stringify here so the 202
    response carries a usable id (aiosqlite unit tests masked this
    because SQLite stores UUIDs as text).
    """
    result = await db.execute(
        select(SyncRun.id)  # type: ignore[attr-defined]
        .where(SyncRun.connector == provider)  # type: ignore[attr-defined]
        .order_by(SyncRun.started_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    run_id = result.scalar_one_or_none()
    return str(run_id) if run_id is not None else None


def _decrypt_config(
    cred: Credential, provider: str, settings: Any
) -> ConnectorConfig:
    """Decrypt a credential row into a ``ConnectorConfig``."""
    raw_payload = decrypt_credential(
        cred.encrypted_payload,
        cred.nonce,
        settings,
    )
    try:
        cred_dict: dict[str, str] = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError):
        cred_dict = {"api_key": raw_payload}
    return ConnectorConfig(provider_type=provider, credentials=cred_dict)


async def _run_provider_sync(
    container: Any,
    db: AsyncSession,
    tenant_id: str,
    provider: str,
    cred: Credential | None,
) -> SyncRunLink:
    """Run one provider's sync; never raises — failures become entries."""
    if cred is None:
        return SyncRunLink(
            provider=provider,
            status="skipped",
            error_message=(
                f"No credentials found for connector {provider!r} "
                f"and tenant {tenant_id}"
            ),
        )
    try:
        config = _decrypt_config(cred, provider, container.settings)
        orchestrator = SyncOrchestrator(
            session_factory=container.session_factory,
            registry=ConnectorRegistry(),
            tenant_id=tenant_id,
            settings=container.settings,
        )
        result = await orchestrator.run_sync(
            provider_type=provider,
            config=config,
        )
        run_id = await _latest_run_id(db, provider)
        return SyncRunLink(
            provider=provider,
            sync_run_id=run_id,
            status=str(result.status.value),
            accounts_synced=result.accounts_synced,
            transactions_synced=result.transactions_synced,
            error_message=result.error_message,
            link=f"/api/v1/sync-runs/{run_id}" if run_id else None,
        )
    except Exception as exc:
        return SyncRunLink(
            provider=provider,
            status="error",
            error_message=str(exc)[:500],
        )


async def _trigger(
    container: Any,
    db: AsyncSession,
    tenant_id: str,
    providers: list[str] | None,
) -> SyncTriggerResponse:
    """Resolve providers and run each sync, returning the 202 payload."""
    cred_result = await db.execute(
        select(Credential).where(  # type: ignore[attr-defined]
            Credential.tenant_id == tenant_id  # type: ignore[attr-defined]
        )
    )
    cred_rows: list[Credential] = list(cred_result.scalars().all())  # type: ignore[assignment]
    cred_by_provider = {c.provider_key: c for c in cred_rows}

    if providers is None:
        providers = sorted(cred_by_provider)

    if not providers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No connectors configured for this tenant",
        )

    links = [
        await _run_provider_sync(
            container, db, tenant_id, provider, cred_by_provider.get(provider)
        )
        for provider in providers
    ]

    return SyncTriggerResponse(
        sync_runs=links,
        meta=CollectionMeta(
            as_of=datetime.now(UTC),
            freshness="fresh",
        ),
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_sync(
    body: SyncTriggerRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
) -> SyncTriggerResponse:
    """Start syncs for the requested (or all configured) connectors."""
    return await _trigger(
        get_container(request), db, auth.tenant_id, body.providers
    )


@router.post(
    "/{provider}",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_sync_provider(
    provider: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
) -> SyncTriggerResponse:
    """Start a sync for one configured provider (registry key)."""
    return await _trigger(
        get_container(request), db, auth.tenant_id, [provider]
    )
