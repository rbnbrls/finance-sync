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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_auth_context,
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
from finance_sync.models.connector_release import ConnectorRelease
from finance_sync.models.credential import (
    CONNECTION_STATUS_ACTIVE,
    CONNECTION_STATUS_PAUSED,
    Credential,
)
from finance_sync.models.transaction import Transaction
from finance_sync.schemas.connector_release import (
    ConnectorReleaseRequest,
    ConnectorReleaseResponse,
    ReleaseStatus,
)
from finance_sync.schemas.provider_health import ProviderHealthOverview
from finance_sync.services.auth import decrypt_credential, encrypt_credential
from finance_sync.services.connection_audit import (
    AUDIT_ACCOUNTS,
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_PAUSE,
    AUDIT_REAUTH_FAILURE,
    AUDIT_REAUTH_START,
    AUDIT_REAUTH_SUCCESS,
    AUDIT_RELEASE_CANDIDATE,
    AUDIT_RESUME,
    AUDIT_TEST,
    AUDIT_UPDATE,
    list_connection_audit_events,
    log_connection_event,
)
from finance_sync.services.connector_compatibility import (
    ConnectorCompatibility,
    default_contract_paths,
    evaluate_connector,
    load_json,
)
from finance_sync.services.connector_data_deletion import (
    ConnectorDataDeletionService,
)
from finance_sync.services.connector_releases import (
    ConnectorReleaseError,
    register_candidate,
)
from finance_sync.services.connector_releases import (
    pause as pause_release,
)
from finance_sync.services.connector_releases import (
    promote as promote_release,
)
from finance_sync.services.connector_releases import (
    resume as resume_release,
)
from finance_sync.services.connector_releases import (
    rollback as rollback_release,
)
from finance_sync.services.provider_health import ProviderHealthService
from finance_sync.utils.redaction import sanitize_error

router = APIRouter(prefix="/connectors", tags=["connectors"])


async def _require_operator(
    auth: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    """Require a human administrator for provider-wide release changes."""
    if auth.user is None or auth.user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connector release actions require an administrator",
        )
    return auth


def _release_response(row: ConnectorRelease) -> ConnectorReleaseResponse:
    return ConnectorReleaseResponse(
        id=str(row.id),
        provider_key=row.provider_key,
        version=row.version,
        status=cast(ReleaseStatus, row.status),
        previous_version=row.previous_version,
        certification_status=row.certification_status,
        certification_commit=row.certification_commit,
        compatibility_status=row.compatibility_status,
        canary_status=row.canary_status,
        capabilities=list(row.capabilities or []),
        reason_code=row.reason_code,
        enabled_at=row.enabled_at,
        disabled_at=row.disabled_at,
    )


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
    spending_capabilities: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        description="Optional spending capabilities and availability",
    )
    configuration_mode: str = Field(
        default="user",
        description="Whether configuration is user-managed or staging-selectable.",
    )
    ingestion_methods: list[str] = Field(
        default_factory=lambda: ["api"],
        description="Supported user-facing ingestion methods: api and/or file",
    )
    import_wizard: dict[str, object] = Field(
        default_factory=dict,
        description="Secret-safe provider-specific import wizard hints",
    )


class ConnectorCatalogInfo(BaseModel):
    """Stable, secret-safe metadata for one installed connector."""

    name: str
    provider_key: str
    display_name: str
    plugin_package: str
    plugin_version: str
    sdk_version: str
    supported_resources: list[str]
    spending_capabilities: dict[str, dict[str, object]] = Field(
        default_factory=dict
    )
    credential_fields: list[dict[str, object]]
    option_fields: list[dict[str, object]]
    rate_limit_policy: dict[str, int | float] | None = None
    auth_mode: str = "credentials"
    documentation_url: str | None = None
    lifecycle_status: str = "available"
    feature_flag: str | None = None
    configuration_mode: str
    ingestion_methods: list[str] = Field(default_factory=lambda: ["api"])
    import_wizard: dict[str, object] = Field(default_factory=dict)
    metadata_incomplete: bool = False
    compatibility: ConnectorCompatibility


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
        description="Sanitised error of the last failed sync",
    )
    last_test_at: datetime | None = None
    last_test_status: str | None = None
    last_test_error: str | None = None
    credential_status: str = "unknown"
    last_authenticated_at: datetime | None = None
    expires_at: datetime | None = None
    reauth_required_at: datetime | None = None
    credential_version: int = 1
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


class ConnectorDeletionPreviewResponse(BaseModel):
    """Impact summary shown before permanently deleting a connection."""

    provider_key: str
    connection_id: str
    accounts: int
    transactions: int
    card_transactions: int
    holdings: int
    balances: int
    other_records: int
    legacy_records_warning: str | None = None


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


def _account_enumeration_error_is_fatal(provider_key: str) -> bool:
    """Return whether account discovery is required to validate a provider.

    Bunq's authenticated session can be created successfully while the
    monetary-account request still fails. Treating that failure as optional
    makes the UI report a successful connection with no accounts, after which
    the worker cannot sync balances or payments. Other connectors retain the
    historical best-effort behaviour because some need extra scopes for
    account enumeration.
    """
    return provider_key == "bunq"


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


class ReauthenticateRequest(BaseModel):
    """Replacement credentials tested before they are committed."""

    credentials: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


# ── Response helpers ───────────────────────────────────────────────────

# Providers whose configs count as configured without encrypted payloads
_NON_SECRET_PROVIDERS = {
    "degiro_pension",
    "saxo_investor",
    "csv_import",
    "manual_expense",
}

# SaxoInvestor is a single-account file source. Its positions and
# transactions exports belong to the same account and must share one profile.
_SINGLE_CONNECTION_PROVIDERS = {"saxo_investor"}


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
    # Older connections stored only the JSON options object in description.
    # Never expose that implementation detail as the connection's name.
    if not label or label.lstrip().startswith("{"):
        metadata = _get_registry().list_connectors().get(row.provider_key, {})
        label = str(metadata.get("display_name", row.provider_key))
    return ConnectorConfigResponse(
        id=str(row.id),
        connection_id=str(row.id),
        provider_type=row.provider_key,
        description=label,
        options=options,
        is_configured=is_configured,
        status=row.status or "active",
        selected_accounts=row.selected_accounts,
        last_attempt_at=row.last_attempt_at,
        last_success_at=row.last_success_at,
        last_error=row.last_error,
        last_test_at=getattr(row, "last_test_at", None),
        last_test_status=getattr(row, "last_test_status", None),
        last_test_error=getattr(row, "last_test_error", None),
        credential_status=getattr(row, "credential_status", None) or "unknown",
        last_authenticated_at=getattr(row, "last_authenticated_at", None),
        expires_at=getattr(row, "expires_at", None),
        reauth_required_at=getattr(row, "reauth_required_at", None),
        credential_version=int(getattr(row, "credential_version", 1) or 1),
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


@router.get(
    "/{connection_id}/health",
    response_model=ProviderHealthOverview,
)
async def get_connection_health(
    connection_id: str,
    request: Request,
    refresh: bool = Query(
        default=False,
        description="Run one lightweight provider health check before projecting health",
    ),
    auth: AuthContext = Depends(require_permission("connectors", "read")),
    db: AsyncSession = Depends(get_db),
) -> ProviderHealthOverview:
    """Return the three-level health projection for one connection.

    A normal read is database-only. ``refresh=true`` performs only the
    connector's lightweight ``health`` hook; it never starts a full sync.
    """
    cred = await _load_tenant_credential(db, auth, connection_id)
    if refresh:
        container = get_container(request)
        credentials: dict[str, str] = {}
        if cred.encrypted_payload:
            try:
                raw = decrypt_credential(
                    cred.encrypted_payload, cred.nonce, container.settings
                )
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    credentials = cast(dict[str, str], parsed)
            except Exception as exc:
                cred.last_error = sanitize_error(str(exc), [])
                cred.last_error_category = "authentication"
        options: dict[str, Any] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed_options = json.loads(cred.description or "{}")
            if isinstance(parsed_options, dict):
                options = cast(dict[str, Any], parsed_options)
        try:
            connector = _get_registry().get_connector(
                ConnectorConfigModel(
                    provider_type=cred.provider_key,
                    credentials=credentials,
                    options=options,
                )
            )
            health = await connector.health()
            now = datetime.now(UTC)
            cred.last_test_at = now
            cred.last_test_status = "passed" if health.healthy else "failed"
            if health.healthy:
                cred.last_error = None
                cred.last_error_category = None
            else:
                cred.last_error = sanitize_error(
                    health.message or "Health check failed",
                    list(credentials.values()),
                )
                cred.last_error_category = "provider_unavailable"
            await db.flush()
        except Exception as exc:
            cred.last_error = sanitize_error(
                str(exc), list(credentials.values())
            )
            cred.last_error_category = "unknown"
            await db.flush()
    return await _single_provider_health(db, auth.tenant_id, cred)


async def _single_provider_health(
    db: AsyncSession, tenant_id: str, credential: Credential
) -> ProviderHealthOverview:
    overviews = await ProviderHealthService(db, tenant_id).get_overview()
    for overview in overviews:
        if overview.connection_id == str(credential.id):
            return overview
    raise HTTPException(status_code=404, detail="Connection health not found")


@router.post(
    "/releases/{provider_key}",
    response_model=ConnectorReleaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_connector_release(
    provider_key: str,
    body: ConnectorReleaseRequest,
    auth: AuthContext = Depends(_require_operator),
    db: AsyncSession = Depends(get_db),
) -> ConnectorReleaseResponse:
    """Register a candidate release; registration never enables it."""
    try:
        release = await register_candidate(
            db,
            provider_key=provider_key,
            version=body.version,
            previous_version=body.previous_version,
            certification_status=body.certification_status,
            certification_commit=body.certification_commit,
            compatibility_status=body.compatibility_status,
            canary_status=body.canary_status,
            capabilities=body.capabilities,
        )
    except ConnectorReleaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await log_connection_event(
        db,
        tenant_id=auth.tenant_id,
        action=AUDIT_RELEASE_CANDIDATE,
        provider_key=provider_key,
        detail={
            "version": release.version,
            "result": "candidate_registered",
            "reason_code": "release_candidate_registered",
        },
        actor_user_id=auth.principal_id,
        actor_role=auth.user.role if auth.user else None,
    )
    return _release_response(release)


async def _release_action(
    db: AsyncSession,
    auth: AuthContext,
    provider_key: str,
    version: str | None,
    action: str,
) -> ConnectorReleaseResponse:
    try:
        if action == "promote":
            assert version is not None
            release = await promote_release(db, provider_key, version)
        elif action == "pause":
            assert version is not None
            release = await pause_release(db, provider_key, version)
        elif action == "resume":
            assert version is not None
            release = await resume_release(db, provider_key, version)
        else:
            release = await rollback_release(db, provider_key)
    except ConnectorReleaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await log_connection_event(
        db,
        tenant_id=auth.tenant_id,
        action=action,
        provider_key=provider_key,
        detail={"version": release.version, "action": action},
        actor_user_id=auth.principal_id,
        actor_role=auth.user.role if auth.user else None,
    )
    return _release_response(release)


@router.post(
    "/releases/{provider_key}/{version}/promote",
    response_model=ConnectorReleaseResponse,
)
async def promote_connector_release(
    provider_key: str,
    version: str,
    auth: AuthContext = Depends(_require_operator),
    db: AsyncSession = Depends(get_db),
) -> ConnectorReleaseResponse:
    return await _release_action(db, auth, provider_key, version, "promote")


@router.post(
    "/releases/{provider_key}/{version}/pause",
    response_model=ConnectorReleaseResponse,
)
async def pause_connector_release(
    provider_key: str,
    version: str,
    auth: AuthContext = Depends(_require_operator),
    db: AsyncSession = Depends(get_db),
) -> ConnectorReleaseResponse:
    return await _release_action(db, auth, provider_key, version, "pause")


@router.post(
    "/releases/{provider_key}/{version}/resume",
    response_model=ConnectorReleaseResponse,
)
async def resume_connector_release(
    provider_key: str,
    version: str,
    auth: AuthContext = Depends(_require_operator),
    db: AsyncSession = Depends(get_db),
) -> ConnectorReleaseResponse:
    return await _release_action(db, auth, provider_key, version, "resume")


@router.post(
    "/releases/{provider_key}/rollback",
    response_model=ConnectorReleaseResponse,
)
async def rollback_connector_release(
    provider_key: str,
    auth: AuthContext = Depends(_require_operator),
    db: AsyncSession = Depends(get_db),
) -> ConnectorReleaseResponse:
    return await _release_action(db, auth, provider_key, None, "rollback")


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
                    "required": True,
                    "description": (
                        "Required for current Trading212 API keys. "
                        "Your secret is encrypted and never shown again."
                    ),
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
                # Browser uploads do not need filesystem paths or a manual
                # snapshot override. The upload wizard supplies a stable
                # account key and uses the profile label as account name.
            ],
        ),
        "saxo_investor": (
            [],
            [
                {
                    "key": "account_key",
                    "label": "Rekeningkenmerk",
                    "type": "text",
                    "default": "default",
                    "description": "Blijvend technisch kenmerk voor deze ene Saxo-rekening.",
                },
                {
                    "key": "account_name",
                    "label": "Rekeningnaam",
                    "type": "text",
                    "default": "SaxoInvestor",
                },
                {
                    "key": "snapshot_at",
                    "label": "Snapshotdatum",
                    "type": "date",
                    "required": False,
                    "description": "Optioneel; overschrijft de datum uit de bestandsnaam.",
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
                spending_capabilities=cast(
                    dict[str, dict[str, object]],
                    meta.get("spending_capabilities", {}),
                ),
                configuration_mode="staging_choice" if managed else "user",
                ingestion_methods=list(meta.get("ingestion_methods", ["api"])),
                import_wizard=cast(
                    dict[str, object], meta.get("import_wizard", {})
                ),
            )
        )
    return result


@router.get("/catalog", response_model=list[ConnectorCatalogInfo])
async def list_connector_catalog(
    request: Request,
) -> list[ConnectorCatalogInfo]:
    """Return the installed connector catalogue without secret values.

    The catalogue is static installation metadata and therefore remains
    available before login, like the legacy ``GET /connectors`` endpoint.
    Tenant-specific connection health is deliberately not included here.
    """
    registry = _get_registry()
    settings = get_container(request).settings
    lifecycle_path, matrix_path = default_contract_paths()
    lifecycle = load_json(lifecycle_path)
    contract_matrix = load_json(matrix_path)
    fixture_versions = {
        str(item["name"]): str(item["fixture_date"])
        for item in cast(
            "list[dict[str, Any]]", contract_matrix.get("connectors", [])
        )
        if item.get("name") and item.get("fixture_date")
    }
    result: list[ConnectorCatalogInfo] = []
    for name, meta in registry.list_connectors().items():
        managed = is_staging_managed(name, settings)
        credential_fields, option_fields = _get_connector_credential_schema(
            name
        )
        if managed:
            credential_fields, option_fields = _staging_connector_schema(name)

        raw_rate_limit = meta.get("rate_limit_policy")
        rate_limit: dict[str, int | float] | None = (
            cast("dict[str, int | float]", raw_rate_limit)
            if isinstance(raw_rate_limit, dict)
            else None
        )
        credential_auth = "file" if not credential_fields else "credentials"
        compatibility = evaluate_connector(
            lifecycle,
            meta,
            fixture_version=fixture_versions.get(name),
            contract_matrix=contract_matrix,
            enabled=bool(
                cast(
                    "dict[str, bool]",
                    getattr(settings, "connector_feature_flags", {}),
                ).get(name, True)
            ),
        )
        result.append(
            ConnectorCatalogInfo(
                name=name,
                provider_key=str(meta.get("provider_key", name)),
                display_name=str(meta.get("display_name", name)),
                plugin_package=str(meta.get("plugin_package", "unknown")),
                plugin_version=str(meta.get("plugin_version", "0.1.0")),
                sdk_version=str(meta.get("sdk_version", "0.1.0")),
                supported_resources=list(meta.get("supported_resources", [])),
                spending_capabilities=cast(
                    dict[str, dict[str, object]],
                    meta.get("spending_capabilities", {}),
                ),
                credential_fields=credential_fields,
                option_fields=option_fields,
                rate_limit_policy=rate_limit,
                auth_mode=str(meta.get("auth_mode", credential_auth)),
                documentation_url=meta.get("documentation_url"),
                lifecycle_status=str(meta.get("lifecycle_status", "available")),
                feature_flag=meta.get("feature_flag"),
                configuration_mode="staging_choice" if managed else "user",
                metadata_incomplete=bool(
                    meta.get("metadata_incomplete", False)
                ),
                ingestion_methods=list(meta.get("ingestion_methods", ["api"])),
                import_wizard=cast(
                    dict[str, object], meta.get("import_wizard", {})
                ),
                compatibility=compatibility,
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


@router.get(
    "/configs/{config_id}/deletion-preview",
    response_model=ConnectorDeletionPreviewResponse,
)
async def preview_connector_deletion(
    config_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorDeletionPreviewResponse:
    """Return the tenant-scoped impact of permanently deleting a connection."""
    cred = await _load_tenant_credential(db, auth, config_id)
    if is_staging_managed(cred.provider_key, get_container(request).settings):
        # Kept as a separate guard in the preview so the UI cannot present a
        # destructive action that the DELETE endpoint will reject.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This connector is staging-managed and cannot be removed",
        )
    preview = await ConnectorDataDeletionService(db, auth.tenant_id).preview(
        cred
    )
    return ConnectorDeletionPreviewResponse(**preview.as_dict())


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

    if body.provider_type in _SINGLE_CONNECTION_PROVIDERS:
        existing = await db.scalar(
            select(Credential.id).where(
                Credential.tenant_id == auth.tenant_id,
                Credential.provider_key == body.provider_type,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Er bestaat al één SaxoInvestor-account voor deze tenant.",
            )

    # Other providers intentionally allow multiple connections per tenant.

    # Encrypt credentials if provided
    encrypted_payload: bytes = b""
    nonce: bytes = b""
    if credentials:
        plaintext = json.dumps(credentials, separators=(",", ":"))
        encrypted_payload, nonce = encrypt_credential(plaintext, settings)

    # Merge human-readable label into options so it survives updates
    merged_options = dict(options)
    default_label = str(
        _get_registry().list_connectors()
        .get(body.provider_type, {})
        .get("display_name", body.provider_type)
    )
    merged_options["_label"] = body.description or default_label

    # Store the merged payload (options + optional _label) in description column
    merged_json = (
        json.dumps(merged_options, separators=(",", ":"))
        if merged_options
        else "{}"
    )

    now = datetime.now(UTC)
    cred = Credential(
        tenant_id=auth.tenant_id,
        owner_user_id=(str(auth.user.id) if auth.user is not None else None),
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

    # New active connections receive an enabled default schedule
    # atomically (same transaction) so they are schedulable immediately.
    # Only schedulable providers (bunq/trading212) get a row; the others
    # keep their own triggers.
    from finance_sync.models.sync_schedule import (
        INGESTION_SCHEDULABLE_PROVIDERS,
        SCOPE_INGESTION,
    )
    from finance_sync.services.sync_schedule import (
        SyncScheduleService,
    )

    if body.provider_type in INGESTION_SCHEDULABLE_PROVIDERS:
        svc = SyncScheduleService(db)
        await svc.ensure_for_scope(
            auth.tenant_id,
            scope=SCOPE_INGESTION,
            target_id=str(cred.id),
            actor_user_id=auth.principal_id,
        )

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

    Deleting a connection permanently removes all canonical and derived
    records owned by that exact connection.  Legacy records without a
    connection id are deliberately preserved.
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
    await ConnectorDataDeletionService(db, auth.tenant_id).delete(cred)

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
    offers so the frontend can drive account selection.  Test metadata is
    stored separately from sync metadata: ``last_success_at`` is reserved
    for a completed import, and ``last_attempt_at`` / ``last_error`` are
    not changed by this authentication check. The attempt is recorded in
    the tenant audit log, and error messages are sanitised before being
    stored or returned.
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
            cred.last_error_category = "authentication"
            cred.last_test_at = now
            cred.last_test_status = "failed"
            cred.last_test_error = failure
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

    # SaxoInvestor is a file-upload connector. A stored connection has no
    # permanent source path, so testing it without an uploaded workbook must
    # not be reported as a broken connection. Keep validating legacy
    # self-hosted configurations that still contain an export path.
    if cred.provider_key == "saxo_investor" and not (
        options.get("export_path") or options.get("export_paths")
    ):
        return ConnectorTestResult(
            success=True,
            message=(
                "SaxoInvestor is ingesteld. Upload een XLSX-exportbestand "
                "om posities en transacties te importeren."
            ),
        )

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
                # Bunq account discovery is required for a useful connection:
                # without it neither balances nor payments can be synced. Other
                # providers retain best-effort enumeration for scope compatibility.
                if _account_enumeration_error_is_fatal(cred.provider_key):
                    raise

        cred.last_test_at = now
        cred.updated_at = now
        if health.healthy:
            cred.credential_status = "verified"
            cred.last_authenticated_at = now
            cred.reauth_required_at = None
            cred.last_auth_error_code = None
            cred.last_test_status = "passed"
            cred.last_test_error = None
            message = health.message or "Connection successful"
        else:
            from finance_sync.sync.errors import categorize_export_error

            test_error = sanitize_error(
                health.message or "Connection test failed", secrets
            )
            test_error_category = categorize_export_error(
                health.message or "Connection test failed"
            )
            cred.credential_status = (
                "reauth_required"
                if test_error_category == "reauth_required"
                else "unknown"
            )
            if cred.credential_status == "reauth_required":
                cred.reauth_required_at = now
                cred.last_auth_error_code = test_error_category
            cred.last_test_status = "failed"
            cred.last_test_error = test_error
            message = test_error or "Connection test failed"
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
        from finance_sync.sync.errors import categorize_export_error

        test_error_category = categorize_export_error(str(exc))
        cred.credential_status = (
            "reauth_required"
            if test_error_category == "reauth_required"
            else "unknown"
        )
        if cred.credential_status == "reauth_required":
            cred.reauth_required_at = now
            cred.last_auth_error_code = test_error_category
        cred.last_test_at = now
        cred.last_test_status = "failed"
        cred.last_test_error = failure
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
    "/{connection_id}/reauthenticate",
    response_model=ConnectorConfigResponse,
)
async def reauthenticate_connector(
    connection_id: str,
    body: ReauthenticateRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Test replacement credentials, then atomically activate them.

    The old encrypted payload is untouched when authentication fails. The
    endpoint never returns, logs, or audits the submitted secret values.
    """
    if not body.credentials:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="New credentials are required",
        )
    container = get_container(request)
    cred = await _load_tenant_credential(db, auth, connection_id)
    registry = _get_registry()
    candidate = ConnectorConfigModel(
        provider_type=cred.provider_key,
        credentials=dict(body.credentials),
        options=dict(body.options),
    )
    try:
        await log_connection_event(
            db,
            tenant_id=auth.tenant_id,
            action=AUDIT_REAUTH_START,
            provider_key=cred.provider_key,
            connection_id=str(cred.id),
            detail={"result": "started", "reason_code": "reauth_requested"},
            actor_user_id=auth.principal_id,
            actor_role=auth.user.role if auth.user else None,
        )
        connector = registry.get_connector(candidate)
        await connector.reauthenticate()
        expires_at = await connector.credential_expiry()
    except Exception as exc:
        from finance_sync.sync.errors import categorize_export_error

        safe_error = sanitize_error(str(exc), list(body.credentials.values()))
        cred.credential_status = "reauth_required"
        cred.reauth_required_at = datetime.now(UTC)
        cred.last_auth_error_code = categorize_export_error(str(exc))
        cred.last_error = safe_error
        cred.last_error_category = cred.last_auth_error_code
        cred.updated_at = datetime.now(UTC)
        await db.flush()
        await log_connection_event(
            db,
            tenant_id=auth.tenant_id,
            action=AUDIT_REAUTH_FAILURE,
            provider_key=cred.provider_key,
            connection_id=str(cred.id),
            detail={
                "success": False,
                "reason_code": cred.last_auth_error_code,
                "error_category": cred.last_auth_error_code,
            },
            actor_user_id=auth.principal_id,
            actor_role=auth.user.role if auth.user else None,
            secrets=list(body.credentials.values()),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=safe_error or "Reauthentication failed",
        ) from exc

    plaintext = json.dumps(body.credentials, separators=(",", ":"))
    encrypted, nonce = encrypt_credential(plaintext, container.settings)
    cred.encrypted_payload = encrypted
    cred.nonce = nonce
    if body.options:
        existing: dict[str, Any] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            parsed = json.loads(cred.description or "{}")
            if isinstance(parsed, dict):
                existing = cast(dict[str, Any], parsed)
        label = existing.get("_label")
        existing = dict(body.options)
        if label:
            existing["_label"] = label
        cred.description = json.dumps(existing, separators=(",", ":"))
    now = datetime.now(UTC)
    cred.credential_status = "verified"
    cred.last_authenticated_at = now
    cred.expires_at = expires_at
    cred.reauth_required_at = None
    cred.last_auth_error_code = None
    cred.last_error = None
    cred.last_error_category = None
    cred.credential_version = (
        int(getattr(cred, "credential_version", 1) or 1) + 1
    )
    cred.updated_at = now
    await db.flush()
    await log_connection_event(
        db,
        tenant_id=auth.tenant_id,
        action=AUDIT_REAUTH_SUCCESS,
        provider_key=cred.provider_key,
        connection_id=str(cred.id),
        detail={"success": True, "reason_code": "reauthenticated"},
        actor_user_id=auth.principal_id,
        actor_role=auth.user.role if auth.user else None,
    )
    return _credential_response(cred)


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
        # Disable the connection's schedule so no scheduled run is
        # planned while paused (the worker also skips paused
        # connections — this keeps next_run_at honest).
        from finance_sync.models.sync_schedule import (
            SCOPE_INGESTION,
            SyncSchedule,
        )

        schedule = (
            await db.execute(
                select(SyncSchedule).where(
                    SyncSchedule.tenant_id == auth.tenant_id,
                    SyncSchedule.scope == SCOPE_INGESTION,
                    SyncSchedule.target_id == str(cred.id),
                )
            )
        ).scalar_one_or_none()
        if schedule is not None:
            schedule.enabled = False
            schedule.next_run_at = None
            schedule.updated_at = datetime.now(UTC)
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
        # Re-enable the connection's schedule (and recompute next_run_at)
        # so scheduled runs resume after resume.
        from finance_sync.models.sync_schedule import (
            SCOPE_INGESTION,
            SyncSchedule,
        )
        from finance_sync.services.sync_schedule import compute_next_run

        schedule = (
            await db.execute(
                select(SyncSchedule).where(
                    SyncSchedule.tenant_id == auth.tenant_id,
                    SyncSchedule.scope == SCOPE_INGESTION,
                    SyncSchedule.target_id == str(cred.id),
                )
            )
        ).scalar_one_or_none()
        if schedule is not None:
            schedule.enabled = True
            instants = compute_next_run(schedule, count=1)
            schedule.next_run_at = instants[0] if instants else None
            schedule.updated_at = datetime.now(UTC)
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
            connection_id=str(e.connection_id) if e.connection_id else None,
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

    if provider_type == "saxo_investor" and not (
        options.get("export_path") or options.get("export_paths")
    ):
        return InlineTestResult(
            success=True,
            message=(
                "SaxoInvestor gebruikt bestanden uit de frontend-upload. "
                "Sla de configuratie op en upload daarna een XLSX-exportbestand."
            ),
        )
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
            # Sanitise the provider's message: an unhealthy connector may
            # echo back the very credentials it was given (bad auth flows
            # often include the key/token in the response text).
            message = sanitize_error(
                health.message or "Connection test failed",
                list(body.credentials.values()),
            )
            return InlineTestResult(success=False, message=message)

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
            # Bunq account discovery is required for a useful connection;
            # without it neither balances nor payments can be synced.
            if _account_enumeration_error_is_fatal(provider_type):
                raise

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
