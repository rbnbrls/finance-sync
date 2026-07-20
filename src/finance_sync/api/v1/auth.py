"""Authentication and authorisation endpoints.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_current_user,
    require_role,
)
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.api_key import ApiKey
from finance_sync.models.user import User as UserModel
from finance_sync.services.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Request / Response schemas ────────────────────────────────────────


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: "UserResponse"


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    is_active: bool
    tenant_id: str


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    permissions: str | None = Field(
        default=None,
        description="Space-separated permission strings, e.g. "
        "'transactions:read'",
    )


class CreateAPIKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    raw_key: str
    permissions: str | None = None


class APIKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    permissions: str | None = None
    is_active: bool
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime


class MeResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    is_active: bool
    tenant_id: str
    permissions: list[str]


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Authenticate with email + password, receive JWT tokens."""
    container = get_container(request)
    settings = container.settings

    # Look up user by email (first match within any tenant)
    result = await db.execute(
        select(UserModel).where(UserModel.email == body.email)
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is deactivated",
        )

    token_data = {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role,
    }
    access_token = create_access_token(token_data, settings)
    refresh_token = create_refresh_token(token_data, settings)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            tenant_id=str(user.tenant_id),
        ),
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
) -> dict[str, Any]:
    """Exchange a valid refresh token for a new access + refresh pair."""
    container = get_container(request)
    settings = container.settings

    try:
        payload: dict[str, Any] = decode_token(body.refresh_token, settings)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from None

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a refresh token",
        )

    token_data = {
        "sub": payload["sub"],
        "tenant_id": payload["tenant_id"],
        "role": payload["role"],
    }
    new_access = create_access_token(token_data, settings)
    new_refresh = create_refresh_token(token_data, settings)

    return RefreshResponse(
        access_token=new_access,
        refresh_token=new_refresh,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    user: UserModel = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the currently authenticated user's profile."""
    from finance_sync.services.auth import ROLE_PERMISSIONS

    perms = sorted(ROLE_PERMISSIONS.get(user.role, set()))

    return MeResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        tenant_id=str(user.tenant_id),
        permissions=perms,
    )


@router.post("/api-keys", response_model=CreateAPIKeyResponse)
async def create_api_key(
    body: CreateAPIKeyRequest,
    request: Request,  # noqa: ARG001
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a new API key for machine-to-machine access.

    Requires ``admin`` role.  The raw key is returned exactly once.
    """
    raw_key, key_hash, prefix = generate_api_key()

    api_key = ApiKey(
        tenant_id=auth.tenant_id,
        user_id=auth.principal_id,
        name=body.name,
        key_prefix=prefix,
        key_hash=key_hash,
        permissions=body.permissions,
    )
    db.add(api_key)
    await db.flush()

    return CreateAPIKeyResponse(
        id=str(api_key.id),
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        raw_key=raw_key,
        permissions=api_key.permissions,
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: str,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke an API key (requires ``admin`` role)."""
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.tenant_id == auth.tenant_id,
        )
    )
    key_row = result.scalar_one_or_none()
    if key_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    key_row.is_active = False
    await db.flush()


@router.get("/api-keys", response_model=list[APIKeyResponse])
async def list_api_keys(
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List all API keys for the current tenant."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.tenant_id == auth.tenant_id)
    )
    keys = result.scalars().all()
    return [
        APIKeyResponse(
            id=str(k.id),
            name=k.name,
            key_prefix=k.key_prefix,
            permissions=k.permissions,
            is_active=k.is_active,
            last_used_at=k.last_used_at,
            expires_at=k.expires_at,
            created_at=k.created_at,
        )
        for k in keys
    ]
