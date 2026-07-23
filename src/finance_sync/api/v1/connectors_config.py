"""REST API for managing connector configurations.

Allows authenticated users to create, read, update, delete, and test
provider connector configurations (credentials + options).

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_role
from finance_sync.connectors.models import (
    ConnectorConfig as ConnectorConfigModel,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.credential import Credential
from finance_sync.services.auth import decrypt_credential, encrypt_credential

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
        default_factory=list,
        description="Optional configuration fields",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Resources this connector can fetch",
    )


class ConnectorConfigResponse(BaseModel):
    """A stored connector configuration (without sensitive credentials)."""

    id: str
    provider_type: str
    description: str | None
    options: dict[str, Any]
    is_configured: bool = Field(
        description="Whether required credentials are populated"
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


class ConnectorTestResult(BaseModel):
    """Result of a connection test."""

    success: bool
    message: str


class InlineTestRequest(BaseModel):
    """Payload for testing a connection with inline
    (not yet saved) credentials.
    """

    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="Provider-specific secrets (API keys, tokens, …)",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Non-secret configuration (sandbox mode, custom endpoints)",
    )


class InlineTestAccount(BaseModel):
    """A single account returned by an inline connection test."""

    id: str = Field(description="Provider account ID")
    label: str = Field(description="Human-readable account label")
    iban: str | None = Field(default=None, description="IBAN if available")


class InlineTestResult(BaseModel):
    """Result of an inline connection test (may include accounts)."""

    success: bool
    message: str
    accounts: list[InlineTestAccount] = Field(
        default_factory=list,
        description=(
            "Accounts available via this connection (if test succeeded)"
        ),
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
    }
    return schemas.get(connector_type, ([], []))


# ── Endpoints ───────────────────────────────────────────────────────────


@router.get("", response_model=list[ConnectorInfo])
async def list_available_connectors() -> list[ConnectorInfo]:
    """List all available connector types with their credential schemas."""
    registry = _get_registry()
    connectors_meta = registry.list_connectors()
    result: list[ConnectorInfo] = []
    for name, meta in connectors_meta.items():
        cred_fields, opt_fields = _get_connector_credential_schema(name)
        capabilities: list[str] = []
        try:
            cls = registry._classes.get(name)  # noqa: SLF001
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
            )
        )
    return result


@router.get("/configs", response_model=list[ConnectorConfigResponse])
async def list_connector_configs(
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorConfigResponse]:
    """List all saved connector configurations for the current tenant."""
    result = await db.execute(
        select(Credential).where(Credential.tenant_id == auth.tenant_id)
    )
    rows = result.scalars().all()
    configs: list[ConnectorConfigResponse] = []
    for row in rows:
        options: dict[str, Any] = {}
        is_configured = bool(row.encrypted_payload)
        label = row.description
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            options = json.loads(row.description or "{}")
            if isinstance(options, dict):
                label = options.pop("_label", label) or label
        configs.append(
            ConnectorConfigResponse(
                id=row.id,
                provider_type=row.provider_key,
                description=label,
                options=options,
                is_configured=is_configured,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return configs


@router.post(
    "/configs",
    response_model=ConnectorConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connector_config(
    body: ConnectorConfigCreate,
    request: Request,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Create a new connector configuration (encrypts credentials)."""
    container = get_container(request)
    settings = container.settings

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

    # Check for existing config of same provider for this tenant
    existing = await db.execute(
        select(Credential).where(
            Credential.tenant_id == auth.tenant_id,
            Credential.provider_key == body.provider_type,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A configuration for "
                f"'{body.provider_type}' already exists. "
                "Use PUT to update it, or DELETE first."
            ),
        )

    # Encrypt credentials if provided
    encrypted_payload: bytes = b""
    nonce: bytes = b""
    if body.credentials:
        plaintext = json.dumps(body.credentials, separators=(",", ":"))
        encrypted_payload, nonce = encrypt_credential(plaintext, settings)

    # Merge human-readable label into options so it survives updates
    merged_options = dict(body.options)
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
        created_at=now,
        updated_at=now,
    )
    db.add(cred)
    await db.flush()

    label = body.description or merged_options.get("_label", "")

    return ConnectorConfigResponse(
        id=cred.id,
        provider_type=cred.provider_key,
        description=label,
        options=body.options,
        is_configured=bool(body.credentials),
        created_at=cred.created_at,
        updated_at=cred.updated_at,
    )


@router.put("/configs/{config_id}", response_model=ConnectorConfigResponse)
async def update_connector_config(
    config_id: str,
    body: ConnectorConfigUpdate,
    request: Request,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorConfigResponse:
    """Update an existing connector configuration."""
    container = get_container(request)
    settings = container.settings

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

    # Update credentials if provided
    if body.credentials is not None:
        if body.credentials:
            plaintext = json.dumps(body.credentials, separators=(",", ":"))
            cred.encrypted_payload, cred.nonce = encrypt_credential(
                plaintext, settings
            )
        else:
            # Clear credentials
            cred.encrypted_payload = b""
            cred.nonce = b""

    # Update options if provided (preserve _label from existing)
    if body.options is not None:
        merged_options = dict(body.options)
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
    if body.description is not None and body.options is None:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            existing = json.loads(cred.description or "{}")
            if isinstance(existing, dict):
                if body.description:
                    existing["_label"] = body.description
                else:
                    existing.pop("_label", None)
                cred.description = json.dumps(existing, separators=(",", ":"))

    cred.updated_at = datetime.now(UTC)
    await db.flush()

    options: dict[str, Any] = {}
    label = cred.description
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        options = json.loads(cred.description or "{}")
        if isinstance(options, dict):
            label = options.pop("_label", label) or label

    return ConnectorConfigResponse(
        id=cred.id,
        provider_type=cred.provider_key,
        description=label,
        options=options,
        is_configured=bool(cred.encrypted_payload),
        created_at=cred.created_at,
        updated_at=cred.updated_at,
    )


@router.delete("/configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector_config(
    config_id: str,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a connector configuration."""
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
    await db.delete(cred)
    await db.flush()


@router.post("/configs/{config_id}/test", response_model=ConnectorTestResult)
async def test_connector_connection(
    config_id: str,
    request: Request,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> ConnectorTestResult:
    """Test a connector configuration by calling its ``health`` method."""
    container = get_container(request)
    settings = container.settings

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

    # Decrypt credentials
    credentials: dict[str, str] = {}
    if cred.encrypted_payload:
        try:
            plaintext = decrypt_credential(
                cred.encrypted_payload, cred.nonce, settings
            )
            credentials = json.loads(plaintext)
        except Exception as exc:
            return ConnectorTestResult(
                success=False,
                message=f"Failed to decrypt credentials: {exc}",
            )

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
        return ConnectorTestResult(
            success=health.healthy,
            message=health.message or "Connection successful",
        )
    except Exception as exc:
        return ConnectorTestResult(
            success=False,
            message=str(exc),
        )


@router.post(
    "/{provider_type}/test",
    response_model=InlineTestResult,
)
async def test_connector_inline(
    provider_type: str,
    body: InlineTestRequest,
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

    # Instantiate connector with inline credentials
    connector_config = ConnectorConfigModel(
        provider_type=provider_type,
        credentials=body.credentials,
        options=body.options,
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
            message=str(exc),
        )
