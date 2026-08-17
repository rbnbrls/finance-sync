"""REST API for managing connector configurations.

Allows authenticated users to create, read, update, delete, and test
provider connector configurations (credentials + options).

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

import contextlib
import json
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    require_permission,
    require_role,
)
from finance_sync.connectors.environment import (
    STAGING_STATIC,
    STAGING_TEST_API,
    is_staging_managed,
    staging_connector_config,
)
from finance_sync.connectors.models import (
    ConnectorConfig as ConnectorConfigModel,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.credential import (
    CONNECTION_STATUS_ACTIVE,
    CONNECTION_STATUS_PAUSED,
    Credential,
)
from finance_sync.models.transaction import Transaction
from finance_sync.services.auth import decrypt_credential, encrypt_credential
from finance_sync.services.connection_audit import (
    AUDIT_ACCOUNTS,
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_PAUSE,
    AUDIT_RESUME,
    AUDIT_TEST,
    AUDIT_UPDATE,
    list_connection_audit_events,
    log_connection_event,
)
from finance_sync.utils.redaction import sanitize_error

router = APIRouter(prefix="/connectors", tags=["connectors"])

# ── Singleton registry ──────────────────────────────────────────────────
_registry: ConnectorRegistry | None = None


def _get_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry()
    return _registry


# ── Pydantic schemas ─────────────────────────────────────────────────────


class ConnectorInfo(BaseModel):
    """Public info about an available connector type."""

    name: str = Field(description="Connector key, e.g. 'bunq'")
    display_name: str = Field(description="Human-readable name")
    sdk_version: str = Field(description="SDK version the connector targets")
    credential_fields: list[dict[str, object]] = Field(
        description="Credential fields the connector requires",
        examples=[
            [
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "required": True,
                }
            ],
        ],
    )
    option_fields: list[dict[str, object]] = Field(
        default_factory=list[dict[str, object]],
        description="Optional configuration fields",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Resources this connector can fetch",
    )
    configuration_mode: str = Field(
        default="user",
        description="Whether configuration is user-managed or staging-selectable.",
    )


class ConnectorConfigResponse(BaseModel):
    """A stored connector configuration (without sensitive credentials).

    One entry per **connection**.  ``id`` is the stable ``connection_id``
    used to scope syncs, cursors and audit entries for this connection.
    """

    id: str
    connection_id: str | None = Field(
        default=None,
        description=(
            "Stable connection id (equal to id; explicit for clarity). "
            "Declared optional to keep the schema backward compatible "
            "with clients built against the single-connection API; the "
            "server always populates it."
        ),
    )
    provider_type: str
    description: str | None
    options: dict[str, Any]
    is_configured: bool = Field(
        description="Whether required credentials are populated"
    )
    status: str = Field(
        default="active",
        description="Connection state: 'active' or 'paused'",
    )
    selected_accounts: list[str] | None = Field(
        default=None,
        description=(
            "Provider account IDs selected for sync; null/empty = sync all"
        ),
    )
    last_attempt_at: datetime | None = Field(
        default=None,
        description="When the last sync attempt for this connection started",
    )
    last_success_at: datetime | None = Field(
        default=None,
        description="When the last successful sync completed",
    )
    last_error: str | None = Field(
        default=None,
        description="Sanitised error of the last failed sync / connection test",
    )
    created_at: datetime
    updated_at: datetime


class ConnectorConfigCreate(BaseModel):
    """Payload for creating or updating a connector configuration."""

    provider_type: str = Field(
        ...,
        description="Connector key, e.g. 'bunq', 'trading212'",
    )
    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="Provider-specific secrets (API keys, tokens, …)",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Non-secret configuration (sandbox mode, custom endpoints)",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable label for this config",
    )


class InlineTestAccount(BaseModel):
    """A single account returned by an inline connection test."""

    id: str = Field(description="Provider account ID")
    label: str = Field(description="Human-readable account label")
    iban: str | None = Field(default=None, description="IBAN if available")


class ConnectorTestResult(BaseModel):
    """Result of a connection test."""

    success: bool
    message: str
    accounts: list[InlineTestAccount] = Field(
        default_factory=list[InlineTestAccount],
        description=(
            "Accounts accessible via this connection; empty when the "
            "provider does not support account enumeration or the test failed"
        ),
    )


class ConnectorAccountsUpdate(BaseModel):
    """Payload for selecting the accounts a connection should sync."""

    account_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Provider account IDs to sync for this connection; an empty "
            "list resets the selection so all offered accounts are synced"
        ),
    )
    purge_unselected: bool = Field(
        default=False,
        description=(
            "When true, locally stored accounts (and their transactions) "
            "that are no longer selected are deleted.  Defaults to false: "
            "changing a selection never removes already-imported history "
            "without this explicit confirmation."
        ),
    )


class ConnectionStatusUpdate(BaseModel):
    """Payload for pause/resume requests."""

    status: str = Field(
        ...,
        description="'paused' pauses the connection; 'active' resumes it",
    )


class ConnectionAuditEntry(BaseModel):
    """One sanitised entry from the tenant-scoped connection audit log."""

    id: str
    connection_id: str | None
    provider_key: str
    action: str
    detail: dict[str, Any]
    actor_user_id: str | None
    actor_role: str | None
    created_at: datetime


class InlineTestRequest(BaseModel):
    """Test a connection with inline (unsaved) credentials."""

    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="Provider-specific secrets (API keys, tokens, …)",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Non-secret configuration (sandbox mode, custom endpoints)",
    )


class InlineTestResult(BaseModel):
    """Result of an inline connection test (may include accounts)."""

    success: bool
    message: str
    accounts: list[InlineTestAccount] = Field(
        default_factory=list[InlineTestAccount],
        description="Accounts accessible via this connection",
    )


class ConnectorConfigUpdate(BaseModel):
    """Payload for updating an existing connector configuration."""

    credentials: dict[str, str] | None = Field(
        default=None,
        description="Provider-specific secrets to update",
    )
    options: dict[str, Any] | None = Field(
        default=None,
        description="Non-secret configuration to update",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable label",
    )


# ── Response helpers ───────────────────────────────────────────────────

# Providers whose configs count as configured without encrypted payloads
_NON_SECRET_PROVIDERS = {"degiro_pension", "csv_import", "manual_expense"}


def _credential_secrets(cred: Credential, settings: Any) -> list[str]:
    """Return the decrypted secret values of a credential for redaction.

    Used to scrub error messages before persisting them on the
    connection row / audit log.  Never returns ciphertext.
    """
    if not cred.encrypted_payload:
        return []
    try:
        plaintext = decrypt_credential(
            cred.encrypted_payload, cred.nonce, settings
        )
        secrets = json.loads(plaintext)
        if isinstance(secrets, dict):
            parsed = cast("dict[str, Any]", secrets)
            return [str(v) for v in parsed.values() if isinstance(v, str)]
    except Exception:
        pass
    return []


def _credential_response(row: Credential) -> ConnectorConfigResponse:
    """Build the public response for a credential row (no secrets)."""
    options: Any = {}
    is_configured = bool(row.encrypted_payload) or row.provider_key in (
        _NON_SECRET_PROVIDERS
    )
    label = row.description
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(row.description or "{}")
        if isinstance(parsed, dict):
            options = cast(dict[str, Any], parsed)
            label = options.pop("_label", label) or label
    return ConnectorConfigResponse(
        id=row.id,
        connection_id=row.id,
        provider_type=row.provider_key,
        description=label,
        options=options,
        is_configured=is_configured,
        status=row.status or "active",
        selected_accounts=row.selected_accounts,
        last_attempt_at=row.last_attempt_at,
        last_success_at=row.last_success_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _load_tenant_credential(
    db: AsyncSession, auth: AuthContext, config_id: str
) -> Credential:
    """Load a tenant-scoped credential row or raise 404."""
    result = await db.execute(
        select(Credential).where(
            Credential.id == config_id,
            Credential.tenant_id == auth.tenant_id,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connector configuration not found",
        )
    return cred


# ── Credential field definitions per connector ──────────────────────────


def _get_connector_credential_schema(
    connector_type: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return the credential and option field schemas for a connector type.

    This is manually defined for built-in connectors. In the future this
    could be driven by the connector's own metadata/descriptor.
    """
    schemas: dict[
        str, tuple[list[dict[str, object]], list[dict[str, object]]]
    ] = {
        "bunq": (
            [
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "placeholder": "Enter your bunq API key",
                    "required": True,
                },
            ],
            [
                {
                    "key": "base_url",
                    "label": "Custom API Base URL",
                    "type": "text",
                    "placeholder": "https://api.bunq.com/v1 (default)",
                    "default": "https://api.bunq.com/v1",
                },
                {
                    "key": "full_auth",
                    "label": "Full installation flow",
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Register an installation/device (required for new "
                        "bunq API keys). Disable only for an already-registered "
                        "installation or static fixtures."
                    ),
                },
                {
                    "key": "permitted_ips",
                    "label": "Permitted IPs (comma-separated)",
                    "type": "text",
                    "placeholder": "e.g. 203.0.113.1, 203.0.113.2",
                    "description": (
                        "IPs allowed to use the device registration. Leave "
                        "blank for keys already restricted server-side."
                    ),
                },
            ],
        ),
        "trading212": (
            [
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "placeholder": "Enter your Trading212 API key",
                    "required": True,
                },
                {
                    "key": "api_secret",
                    "label": "API Secret",
                    "type": "password",
                    "placeholder": "Enter your Trading212 API secret",
                    "required": False,
                    "description": "Required for current Trading212 API keys",
                },
            ],
            [
                {
                    "key": "demo",
                    "label": "Demo Mode",
                    "type": "boolean",
                    "default": False,
                    "description": "Use the demo API instead of live",
                },
                {
                    "key": "base_url",
                    "label": "Custom API Base URL",
                    "type": "text",
                    "placeholder": "https://live.trading212.com (default)",
                },
            ],
        ),
        "degiro_pension": (
            [],
            [
                {
                    "key": "watchfolder",
                    "label": "Inkomende watchfolder",
                    "type": "text",
                    "required": False,
                    "description": (
                        "Alleen voor self-hosting; mount deze map ook in de worker"
                    ),
                },
                {
                    "key": "archive_directory",
                    "label": "Archiefmap",
                    "type": "text",
                    "required": False,
                },
                {
                    "key": "quarantine_directory",
                    "label": "Quarantainemap",
                    "type": "text",
                    "required": False,
                },
                {
                    "key": "account_key",
                    "label": "Rekeningkenmerk",
                    "type": "text",
                    "required": False,
                    "description": (
                        "Willekeurig, blijvend kenmerk; gebruik geen "
                        "gebruikersnaam of rekeningnummer"
                    ),
                },
                {
                    "key": "account_name",
                    "label": "Rekeningnaam",
                    "type": "text",
                    "default": "DEGIRO Pensioen",
                },
                {
                    "key": "snapshot_at",
                    "label": "Portefeuillesnapshotdatum",
                    "type": "date",
                    "required": False,
                },
            ],
        ),
    }
    return schemas.get(connector_type, ([], []))


def _staging_connector_schema(
    connector_type: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Fields for choosing static fixtures or an official test API."""
    credentials, _ = _get_connector_credential_schema(connector_type)
    for field in credentials:
        field["required"] = False
    return credentials, [
        {
            "key": "data_source",
            "label": "Staging data source",
            "type": "select",
            "default": STAGING_STATIC,
            "choices": [
                {"value": STAGING_STATIC, "label": "Static test dataset"},
                {
                    "value": STAGING_TEST_API,
                    "label": (
                        "bunq Sandbox"
                        if connector_type == "bunq"
                        else "Trading212 Paper Trading"
                    ),
                },
            ],
            "description": (
                "Test API credentials are required only when that option "
                "is selected."
            ),
        }
    ]


# ── Endpoints ───────────────────────────────────────────────────────────


@router.get("", response_model=list[ConnectorInfo])
async def list_available_connectors(
    request: Request,
) -> list[ConnectorInfo]:
    """List all available connector types with their credential schemas."""
    registry = _get_registry()
    settings = get_container(request).settings
    connectors_meta = registry.list_connectors()
    result: list[ConnectorInfo] = []
    for name, meta in connectors_meta.items():
        managed = is_staging_managed(name, settings)
        cred_fields, opt_fields = _get_connector_credential_schema(name)
        if managed:
            cred_fields, opt_fields = _staging_connector_schema(name)
        capabilities: list[str] = []
        try:
            cls = registry._classes.get(name)  # type: ignore[attr-defined]
            if cls and hasattr(cls, "supported_resources"):
                capabilities = sorted(cls.supported_resources)  # type: ignore[attr-defined]
        except Exception:
            pass
        result.append(
            ConnectorInfo(
                name=name,
                display_name=meta.get("display_name", name),
                sdk_version=meta.get("sdk_version", "0.1.0"),
                credential_fields=cred_fields,
                option_fields=opt_fields,
                capabilities=capabilities,
                configuration_mode="staging_choice" if managed else "user",
            )
        )
    return result


@router.get("/configs", response_model=list[ConnectorConfigResponse])
async def list_connector_configs(
    auth: AuthContext = Depends(require_permission("connectors", "read")),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorConfigResponse]:
    """List all saved connector configurations (connections) for the tenant.

    Multiple connections per provider are returned; credentials are never
    included.  Each entry carries its connection status, selected
    accounts and last sync outcome.
    """
    result = await db.execute(
        select(Credential).where(Credential.tenant_id == auth.tenant_id)
    )
    rows = result.scalars().all()
    return [_credential_response(row) for row in rows]


@router.get("/configs/{config_id}", response_model=ConnectorConfigResponse)
async def get_connector_config(
    config_id: str,
    auth: AuthContext = Depends(require_permission("connectors", "read")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Return a single connection by its ``connection_id`` (tenant-scoped).

    Credentials are never included; the response carries the connection's
    status, selected accounts and last sync outcome so the UI can render
    one connection without re-fetching the full list.
    """
    cred = await _load_tenant_credential(db, auth, config_id)
    return _credential_response(cred)


@router.post(
    "/configs",
    response_model=ConnectorConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connector_config(
    body: ConnectorConfigCreate,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Create a new connector configuration (encrypts credentials)."""
    container = get_container(request)
    settings = container.settings

    credentials = body.credentials
    options = body.options
    if is_staging_managed(body.provider_type, settings):
        try:
            credentials, options = staging_connector_config(
                body.provider_type,
                settings,
                data_source=str(
                    body.options.get("data_source", STAGING_STATIC)
                ),
                credentials=body.credentials,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    # Validate provider_type exists
    registry = _get_registry()
    if body.provider_type not in registry:
        available = registry.available
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown connector '{body.provider_type}'. "
                f"Available: {available}"
            ),
        )

    # Multiple connections per provider are allowed — no uniqueness check
    # on (tenant, provider).  Each create call adds a new connection.

    # Encrypt credentials if provided
    encrypted_payload: bytes = b""
    nonce: bytes = b""
    if credentials:
        plaintext = json.dumps(credentials, separators=(",", ":"))
        encrypted_payload, nonce = encrypt_credential(plaintext, settings)

    # Merge human-readable label into options so it survives updates
    merged_options = dict(options)
    if body.description:
        merged_options["_label"] = body.description
    elif "_label" in merged_options:
        # Strip stale label if description was cleared
        del merged_options["_label"]

    # Store the merged payload (options + optional _label) in description column
    merged_json = (
        json.dumps(merged_options, separators=(",", ":"))
        if merged_options
        else "{}"
    )

    now = datetime.now(UTC)
    cred = Credential(
        tenant_id=auth.tenant_id,
        provider_key=body.provider_type,
        encrypted_payload=encrypted_payload,
        nonce=nonce,
        description=merged_json,
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(cred)
    await db.flush()

    await log_connection_event(
        db,
        tenant_id=auth.tenant_id,
        action=AUDIT_CREATE,
        provider_key=cred.provider_key,
        connection_id=str(cred.id),
        detail={
            "label": body.description or merged_options.get("_label", ""),
            "is_configured": bool(credentials),
        },
        actor_user_id=auth.principal_id,
        actor_role=auth.user.role if auth.user else None,
        secrets=list(credentials.values()),
    )

    return _credential_response(cred)


@router.put("/configs/{config_id}", response_model=ConnectorConfigResponse)
async def update_connector_config(
    config_id: str,
    body: ConnectorConfigUpdate,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Update an existing connector configuration."""
    container = get_container(request)
    settings = container.settings

    cred = await _load_tenant_credential(db, auth, config_id)

    credentials_update = body.credentials
    options_update = body.options
    if is_staging_managed(cred.provider_key, settings):
        existing_options: dict[str, Any] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed_existing = json.loads(cred.description or "{}")
            if isinstance(parsed_existing, dict):
                existing_options = cast(dict[str, Any], parsed_existing)
        previous_source = str(
            existing_options.get("data_source", STAGING_STATIC)
        )
        requested_source = str(
            (body.options or {}).get("data_source", previous_source)
        )
        supplied_credentials = body.credentials or {}
        if requested_source == STAGING_TEST_API and not supplied_credentials:
            if previous_source != STAGING_TEST_API:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Test API credentials are required",
                )
            plaintext = decrypt_credential(
                cred.encrypted_payload, cred.nonce, settings
            )
            supplied_credentials = json.loads(plaintext)
        try:
            credentials_update, options_update = staging_connector_config(
                cred.provider_key,
                settings,
                data_source=requested_source,
                credentials=supplied_credentials,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    # Update credentials if provided
    if credentials_update is not None:
        if credentials_update:
            plaintext = json.dumps(credentials_update, separators=(",", ":"))
            cred.encrypted_payload, cred.nonce = encrypt_credential(
                plaintext, settings
            )
        else:
            # Clear credentials
            cred.encrypted_payload = b""
            cred.nonce = b""

    # Update options if provided (preserve _label from existing)
    if options_update is not None:
        merged_options = dict(options_update)
        if body.description is not None:
            if body.description:
                merged_options["_label"] = body.description
            elif "_label" in merged_options:
                del merged_options["_label"]
        else:
            # Preserve existing _label
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                existing = json.loads(cred.description or "{}")
                if isinstance(existing, dict) and "_label" in existing:
                    merged_options["_label"] = existing["_label"]
        cred.description = json.dumps(merged_options, separators=(",", ":"))

    # Update description label (standalone, when options unchanged)
    if body.description is not None and options_update is None:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            existing = json.loads(cred.description or "{}")
            if isinstance(existing, dict):
                if body.description:
                    existing["_label"] = body.description
                else:
                    cast(dict[str, Any], existing).pop("_label", None)
                cred.description = json.dumps(existing, separators=(",", ":"))

    cred.updated_at = datetime.now(UTC)
    await db.flush()

    await log_connection_event(
        db,
        tenant_id=auth.tenant_id,
        action=AUDIT_UPDATE,
        provider_key=cred.provider_key,
        connection_id=str(cred.id),
        detail={
            "label": body.description,
            "credentials_updated": bool(body.credentials),
            "options_updated": bool(body.options),
        },
        actor_user_id=auth.principal_id,
        actor_role=auth.user.role if auth.user else None,
        secrets=list(body.credentials.values()) if body.credentials else [],
    )

    return _credential_response(cred)


@router.delete("/configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector_config(
    config_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a connector configuration (connection).

    Deleting a connection stops future syncs for it but never removes
    already-imported accounts, transactions or holdings — history is
    kept (with the connection id retained for traceability).
    """
    cred = await _load_tenant_credential(db, auth, config_id)
    settings = get_container(request).settings
    if is_staging_managed(cred.provider_key, settings):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{cred.provider_key} is staging-managed and cannot be "
                "removed; edit it to switch data source"
            ),
        )
    provider_key = cred.provider_key
    connection_id = str(cred.id)
    await db.delete(cred)
    await db.flush()

    await log_connection_event(
        db,
        tenant_id=auth.tenant_id,
        action=AUDIT_DELETE,
        provider_key=provider_key,
        connection_id=connection_id,
        detail={"deleted": True},
        actor_user_id=auth.principal_id,
        actor_role=auth.user.role if auth.user else None,
    )


@router.post("/configs/{config_id}/test", response_model=ConnectorTestResult)
async def test_connector_connection(
    config_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorTestResult:
    """Test a connection by calling the connector's ``health`` method.

    On success the returned payload includes the accounts the provider
    offers so the frontend can drive account selection.  The connection's
    ``last_attempt_at`` / ``last_success_at`` / ``last_error`` fields
    are updated and the attempt is recorded in the tenant audit log.
    Error messages are sanitised before being stored or returned.
    """
    container = get_container(request)
    settings = container.settings

    cred = await _load_tenant_credential(db, auth, config_id)
    secrets = _credential_secrets(cred, settings)
    now = datetime.now(UTC)

    # Decrypt credentials
    credentials: dict[str, str] = {}
    if cred.encrypted_payload:
        try:
            plaintext = decrypt_credential(
                cred.encrypted_payload, cred.nonce, settings
            )
            credentials = json.loads(plaintext)
        except Exception as exc:
            failure = sanitize_error(
                f"Failed to decrypt credentials: {exc}", secrets
            )
            cred.last_attempt_at = now
            cred.last_error = failure
            cred.updated_at = now
            await db.flush()
            await log_connection_event(
                db,
                tenant_id=auth.tenant_id,
                action=AUDIT_TEST,
                provider_key=cred.provider_key,
                connection_id=str(cred.id),
                detail={"success": False, "error": failure},
                actor_user_id=auth.principal_id,
                actor_role=auth.user.role if auth.user else None,
                secrets=secrets,
            )
            return ConnectorTestResult(success=False, message=failure)

    # Parse options
    options: dict[str, Any] = {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        options = json.loads(cred.description or "{}")

    # Instantiate connector and test
    registry = _get_registry()
    try:
        connector_config = ConnectorConfigModel(
            provider_type=cred.provider_key,
            credentials=credentials,
            options=options,
        )
        connector = registry.get_connector(connector_config)
        health = await connector.health()

        accounts: list[InlineTestAccount] = []
        if health.healthy:
            # Offer the provider's accounts for the selection UI.
            try:
                raw_accounts = await connector.fetch_accounts()
                for acc in raw_accounts:
                    iban = None
                    if acc.provider_metadata:
                        iban = acc.provider_metadata.get("iban")
                    accounts.append(
                        InlineTestAccount(
                            id=acc.external_account_id,
                            label=acc.name,
                            iban=iban,
                        )
                    )
            except Exception:
                # Account enumeration is optional — don't fail the test.
                pass

        cred.last_attempt_at = now
        cred.updated_at = now
        if health.healthy:
            cred.last_success_at = now
            cred.last_error = None
            message = health.message or "Connection successful"
        else:
            cred.last_error = sanitize_error(
                health.message or "Connection test failed", secrets
            )
            message = cred.last_error or "Connection test failed"
        await db.flush()
        await log_connection_event(
            db,
            tenant_id=auth.tenant_id,
            action=AUDIT_TEST,
            provider_key=cred.provider_key,
            connection_id=str(cred.id),
            detail={"success": health.healthy, "message": message},
            actor_user_id=auth.principal_id,
            actor_role=auth.user.role if auth.user else None,
            secrets=secrets,
        )
        return ConnectorTestResult(
            success=health.healthy,
            message=message,
            accounts=accounts,
        )
    except Exception as exc:
        failure = sanitize_error(str(exc), secrets)
        cred.last_attempt_at = now
        cred.last_error = failure
        cred.updated_at = now
        await db.flush()
        await log_connection_event(
            db,
            tenant_id=auth.tenant_id,
            action=AUDIT_TEST,
            provider_key=cred.provider_key,
            connection_id=str(cred.id),
            detail={"success": False, "error": failure},
            actor_user_id=auth.principal_id,
            actor_role=auth.user.role if auth.user else None,
            secrets=secrets,
        )
        return ConnectorTestResult(success=False, message=failure)


@router.post(
    "/configs/{config_id}/pause",
    response_model=ConnectorConfigResponse,
)
async def pause_connector_connection(
    config_id: str,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Pause a connection: the scheduler will skip it until resumed.

    Existing data is kept untouched; only future automatic syncs stop.
    """
    cred = await _load_tenant_credential(db, auth, config_id)
    if cred.status != CONNECTION_STATUS_PAUSED:
        cred.status = CONNECTION_STATUS_PAUSED
        cred.updated_at = datetime.now(UTC)
        await db.flush()
        await log_connection_event(
            db,
            tenant_id=auth.tenant_id,
            action=AUDIT_PAUSE,
            provider_key=cred.provider_key,
            connection_id=str(cred.id),
            detail={"status": CONNECTION_STATUS_PAUSED},
            actor_user_id=auth.principal_id,
            actor_role=auth.user.role if auth.user else None,
        )
    return _credential_response(cred)


@router.post(
    "/configs/{config_id}/resume",
    response_model=ConnectorConfigResponse,
)
async def resume_connector_connection(
    config_id: str,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Resume a paused connection: automatic syncs restart."""
    cred = await _load_tenant_credential(db, auth, config_id)
    if cred.status != CONNECTION_STATUS_ACTIVE:
        cred.status = CONNECTION_STATUS_ACTIVE
        cred.updated_at = datetime.now(UTC)
        await db.flush()
        await log_connection_event(
            db,
            tenant_id=auth.tenant_id,
            action=AUDIT_RESUME,
            provider_key=cred.provider_key,
            connection_id=str(cred.id),
            detail={"status": CONNECTION_STATUS_ACTIVE},
            actor_user_id=auth.principal_id,
            actor_role=auth.user.role if auth.user else None,
        )
    return _credential_response(cred)


@router.post(
    "/configs/{config_id}/accounts",
    response_model=ConnectorConfigResponse,
)
async def set_connection_accounts(
    config_id: str,
    body: ConnectorAccountsUpdate,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Select the provider accounts a connection should sync.

    Only selected accounts are synced and exported to Wealthfolio
    afterwards.  Changing the selection never deletes already-imported
    history unless ``purge_unselected`` is explicitly set to true — the
    purge then removes the locally stored accounts (and their
    transactions) that are no longer selected.
    """
    cred = await _load_tenant_credential(db, auth, config_id)
    previous = list(cred.selected_accounts or [])
    cred.selected_accounts = body.account_ids or None
    cred.updated_at = datetime.now(UTC)
    await db.flush()

    if body.purge_unselected:
        deselected = [
            acc for acc in previous if acc not in (body.account_ids or [])
        ]
        if deselected:
            from finance_sync.models.account import Account

            # Remove the no-longer-selected accounts and their data.
            # Transactions/balances referencing them cascade is NOT
            # automatic — explicit deletes keep the operation auditable.
            accounts = await db.scalars(
                select(Account).where(
                    Account.tenant_id == auth.tenant_id,
                    Account.connection_id == str(cred.id),
                    Account.external_account_id.in_(deselected),
                )
            )
            for account in accounts:
                await db.execute(
                    sa_delete(Transaction).where(
                        Transaction.account_id == account.id
                    )
                )
                await db.delete(account)
            await db.flush()

    await log_connection_event(
        db,
        tenant_id=auth.tenant_id,
        action=AUDIT_ACCOUNTS,
        provider_key=cred.provider_key,
        connection_id=str(cred.id),
        detail={
            "selected_accounts": body.account_ids,
            "purged_unselected": body.purge_unselected,
            "removed_accounts": [
                acc for acc in previous if acc not in (body.account_ids or [])
            ]
            if body.purge_unselected
            else [],
        },
        actor_user_id=auth.principal_id,
        actor_role=auth.user.role if auth.user else None,
    )
    return _credential_response(cred)


@router.get(
    "/audit-log",
    response_model=list[ConnectionAuditEntry],
)
async def list_connection_audit(
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    connection_id: str | None = None,
    provider_key: str | None = None,
    limit: int = 100,
) -> list[ConnectionAuditEntry]:
    """Return the tenant's connection-lifecycle audit trail (admin only).

    Entries are sanitised at write time and never contain credentials or
    financial payloads.
    """
    entries = await list_connection_audit_events(
        db,
        tenant_id=auth.tenant_id,
        connection_id=connection_id,
        provider_key=provider_key,
        limit=min(max(limit, 1), 500),
    )
    return [
        ConnectionAuditEntry(
            id=str(e.id),
            connection_id=e.connection_id,
            provider_key=e.provider_key,
            action=e.action,
            detail=dict(e.detail or {}),
            actor_user_id=e.actor_user_id,
            actor_role=e.actor_role,
            created_at=e.created_at,
        )
        for e in entries
    ]


@router.post(
    "/{provider_type}/test",
    response_model=InlineTestResult,
)
async def test_connector_inline(
    provider_type: str,
    body: InlineTestRequest,
    request: Request,
    _auth: AuthContext = Depends(require_permission("connectors", "write")),
) -> InlineTestResult:
    """Test a connector connection with inline (not yet saved) credentials.

    Used by the frontend to validate credentials before saving a config.
    Can optionally return a list of available accounts when the provider
    supports account enumeration (e.g. bunq).
    """
    registry = _get_registry()

    # Validate provider exists
    if provider_type not in registry:
        available = registry.available
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown connector '{provider_type}'. Available: {available}"
            ),
        )

    settings = get_container(request).settings
    credentials = body.credentials
    options = body.options
    if is_staging_managed(provider_type, settings):
        try:
            credentials, options = staging_connector_config(
                provider_type,
                settings,
                data_source=str(
                    body.options.get("data_source", STAGING_STATIC)
                ),
                credentials=body.credentials,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    # Instantiate connector with inline credentials in user-managed
    # environments, or the synthetic config enforced by staging.
    connector_config = ConnectorConfigModel(
        provider_type=provider_type,
        credentials=credentials,
        options=options,
    )

    try:
        connector = registry.get_connector(connector_config)
        health = await connector.health()

        if not health.healthy:
            return InlineTestResult(
                success=False,
                message=health.message or "Connection test failed",
            )

        # Optionally fetch accounts to return to the caller
        accounts: list[InlineTestAccount] = []
        try:
            raw_accounts = await connector.fetch_accounts()
            for acc in raw_accounts:
                iban = None
                if acc.provider_metadata:
                    iban = acc.provider_metadata.get("iban")
                accounts.append(
                    InlineTestAccount(
                        id=acc.external_account_id,
                        label=acc.name,
                        iban=iban,
                    )
                )
        except Exception:
            # Account listing is optional — don't fail the test if
            # accounts can't be fetched (e.g. Trading212 may need
            # additional scopes)
            pass

        return InlineTestResult(
            success=True,
            message="Connection successful",
            accounts=accounts,
        )
    except Exception as exc:
        return InlineTestResult(
            success=False,
            message=sanitize_error(str(exc), list(body.credentials.values())),
        )
