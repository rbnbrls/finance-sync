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
from typing import Any, cast

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
    """Per-connection sync outcome with a link to the sync run."""

    connection_id: str | None = None
    provider: str
    sync_run_id: str | None = None
    status: str
    accounts_synced: int = 0
    transactions_synced: int = 0
    holdings_synced: int = 0
    unresolved_securities: int = 0
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


async def _latest_run_id(
    db: AsyncSession, provider: str, connection_id: str | None = None
) -> str | None:
    """Return the most recent sync-run id for a connector (optionally
    scoped to one connection).

    ``SyncRun.id`` is a UUID column; on PostgreSQL the ORM returns a
    ``uuid.UUID`` object, which pydantic refuses to coerce into the
    ``str`` fields of ``SyncRunLink`` — stringify here so the 202
    response carries a usable id (aiosqlite unit tests masked this
    because SQLite stores UUIDs as text).
    """
    stmt = select(SyncRun.id)  # type: ignore[attr-defined]
    stmt = stmt.where(SyncRun.connector == provider)  # type: ignore[attr-defined]
    if connection_id is not None:
        stmt = stmt.where(  # type: ignore[attr-defined]
            SyncRun.connection_id == connection_id  # type: ignore[attr-defined]
        )
    stmt = stmt.order_by(SyncRun.started_at.desc()).limit(1)  # type: ignore[attr-defined]
    result = await db.execute(stmt)
    run_id = result.scalar_one_or_none()
    return str(run_id) if run_id is not None else None


def _decrypt_config(
    cred: Credential, provider: str, settings: Any
) -> ConnectorConfig:
    """Decrypt a credential row into a ``ConnectorConfig``.

    The stable connection id and the selected provider accounts travel
    with the config so every sync path (scheduler, API, MCP) scopes the
    run to the exact connection it was triggered for.
    """
    raw_payload = decrypt_credential(
        cred.encrypted_payload,
        cred.nonce,
        settings,
    )
    try:
        cred_dict: dict[str, str] = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError):
        cred_dict = {"api_key": raw_payload}
    options: dict[str, Any] = {}
    try:
        stored = json.loads(getattr(cred, "description", None) or "{}")
        if isinstance(stored, dict):
            options = {
                k: v
                for k, v in cast(dict[str, Any], stored).items()
                if k != "_label"
            }
    except (json.JSONDecodeError, TypeError):
        pass
    return ConnectorConfig(
        provider_type=provider,
        credentials=cred_dict,
        options=options,
        connection_id=str(cred.id),
        selected_accounts=list(cred.selected_accounts or []),
    )


async def _record_sync_audit(
    db: AsyncSession,
    *,
    tenant_id: str,
    cred: Credential,
    status: str,
    error_message: str | None,
) -> None:
    """Append a sanitised sync-trigger entry to the connection audit log."""
    from finance_sync.services.connection_audit import (
        AUDIT_SYNC,
        log_connection_event,
    )

    await log_connection_event(
        db,
        tenant_id=tenant_id,
        action=AUDIT_SYNC,
        provider_key=cred.provider_key,
        connection_id=str(cred.id),
        detail={
            "status": status,
            "error": (error_message or "")[:200],
        },
        secrets=_credential_secrets(db, cred),
    )


def _credential_secrets(db: AsyncSession, cred: Credential) -> list[str]:
    """Best-effort secret values for audit redaction (never raises)."""
    try:
        raw = decrypt_credential(
            cred.encrypted_payload, cred.nonce, db.info.get("settings")
        )
        parsed: dict[str, Any] = json.loads(raw)
        return [str(v) for v in parsed.values() if isinstance(v, str)]
    except Exception:
        pass
    return []


async def _run_connection_sync(
    container: Any,
    db: AsyncSession,
    tenant_id: str,
    cred: Credential,
    *,
    allow_paused: bool = False,
) -> SyncRunLink:
    """Run one connection's sync; never raises — failures become entries.

    The run is scoped to the connection: the orchestrator persists
    connection_id on accounts/transactions/runs/cursors and updates the
    connection's ``last_attempt_at`` / ``last_success_at`` /
    sanitised ``last_error``.
    """
    from finance_sync.models.credential import CONNECTION_STATUS_PAUSED

    if (
        cred.status or "active"
    ) == CONNECTION_STATUS_PAUSED and not allow_paused:
        return SyncRunLink(
            connection_id=str(cred.id),
            provider=cred.provider_key,
            status="skipped",
            error_message="Connection is paused",
        )
    try:
        config = _decrypt_config(cred, cred.provider_key, container.settings)
        orchestrator = SyncOrchestrator(
            session_factory=container.session_factory,
            registry=ConnectorRegistry(),
            tenant_id=tenant_id,
            settings=container.settings,
        )
        result = await orchestrator.run_sync(
            provider_type=cred.provider_key,
            config=config,
            connection_id=str(cred.id),
            selected_accounts=list(cred.selected_accounts or []),
        )
        run_id = await _latest_run_id(db, cred.provider_key, str(cred.id))
        status = str(result.status.value)
        await _record_sync_audit(
            db,
            tenant_id=tenant_id,
            cred=cred,
            status=status,
            error_message=result.error_message,
        )
        return SyncRunLink(
            connection_id=str(cred.id),
            provider=cred.provider_key,
            sync_run_id=run_id,
            status=status,
            accounts_synced=result.accounts_synced,
            transactions_synced=result.transactions_synced,
            holdings_synced=getattr(result, "holdings_synced", 0),
            unresolved_securities=getattr(result, "unresolved_securities", 0),
            error_message=result.error_message,
            link=f"/api/v1/sync-runs/{run_id}" if run_id else None,
        )
    except Exception as exc:
        await _record_sync_audit(
            db,
            tenant_id=tenant_id,
            cred=cred,
            status="error",
            error_message=str(exc),
        )
        return SyncRunLink(
            connection_id=str(cred.id),
            provider=cred.provider_key,
            status="error",
            error_message=str(exc)[:500],
        )


async def _trigger(
    container: Any,
    db: AsyncSession,
    tenant_id: str,
    providers: list[str] | None,
) -> SyncTriggerResponse:
    """Resolve connections and run each one, returning the 202 payload.

    A tenant can hold several connections per provider; every connection
    is synced independently and a failing connection never blocks the
    others.  Paused connections are skipped (they can still be synced
    individually via ``POST /sync/connections/{connection_id}``).
    """
    cred_result = await db.execute(
        select(Credential).where(  # type: ignore[attr-defined]
            Credential.tenant_id == tenant_id  # type: ignore[attr-defined]
        )
    )
    cred_rows: list[Credential] = list(cred_result.scalars().all())  # type: ignore[assignment]

    if providers is None:
        providers = sorted({c.provider_key for c in cred_rows})

    if not providers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No connectors configured for this tenant",
        )

    target_creds = [
        cred for cred in cred_rows if cred.provider_key in providers
    ]

    links = [
        await _run_connection_sync(container, db, tenant_id, cred)
        for cred in target_creds
    ]

    # A requested provider without any configured connection still gets
    # a skipped entry so operators see why nothing ran.
    configured = {cred.provider_key for cred in cred_rows}
    links.extend(
        SyncRunLink(
            provider=provider,
            status="skipped",
            error_message=(
                f"No credentials found for connector {provider!r} "
                f"and tenant {tenant_id}"
            ),
        )
        for provider in providers
        if provider not in configured
    )

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
    """Start syncs for the requested (or all configured) connectors.

    A tenant can hold several connections per provider.  Every active
    connection is processed **independently**: a failing connection
    never blocks its siblings, and each connection's accounts, sync
    runs and cursors stay scoped to it.  Paused connections are
    skipped by this provider-wide trigger (they can still be synced
    individually via ``POST /sync/connections/{connection_id}``).
    Errors are recorded per connection as a sanitised ``last_error``.
    """
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
    """Start a sync for one configured provider (registry key).

    Every connection of the provider is synced independently; paused
    connections are skipped.
    """
    return await _trigger(
        get_container(request), db, auth.tenant_id, [provider]
    )


@router.post(
    "/connections/{connection_id}",
    response_model=SyncRunLink,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_sync_connection(
    connection_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
) -> SyncRunLink:
    """Manually sync a single connection by its stable ``connection_id``.

    Only the given connection is synced: accounts, transactions, sync
    runs and cursors are scoped to it, and its ``last_attempt_at`` /
    ``last_success_at`` / sanitised ``last_error`` are updated.  Unlike
    the provider-wide trigger, an explicit per-connection sync also runs
    for a paused connection (the user asked for this exact connection).
    A missing or foreign connection returns 404.
    """
    result = await db.execute(
        select(Credential).where(  # type: ignore[attr-defined]
            Credential.id == connection_id,  # type: ignore[attr-defined]
            Credential.tenant_id == auth.tenant_id,  # type: ignore[attr-defined]
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connector configuration not found",
        )

    link = await _run_connection_sync(
        get_container(request),
        db,
        auth.tenant_id,
        cred,
        allow_paused=True,
    )
    await db.flush()
    return link
