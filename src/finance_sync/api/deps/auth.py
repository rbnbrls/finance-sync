"""FastAPI dependencies for authentication and authorisation.

WARNING
------
``from __future__ import annotations`` is intentionally omitted here
because FastAPI needs to introspect the function signatures at runtime
when generating OpenAPI schemas.  Type hints are resolved eagerly.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.dependencies import get_container, get_db
from finance_sync.models.api_key import ApiKey
from finance_sync.models.user import User as UserModel
from finance_sync.services.auth import (
    api_key_has_permission,
    decode_token,
    user_has_permission,
    verify_api_key,
)

# ── Bearer token scheme (auto-returns 401 on missing/invalid) ────────

_bearer = HTTPBearer(auto_error=False)

# ── Exception messages ───────────────────────────────────────────────

_MSG_MISSING_BEARER = "Missing Bearer token"
_MSG_INVALID_TOKEN = "Invalid or expired token"
_MSG_NOT_ACCESS_TOKEN = "Token is not an access token"
_MSG_MISSING_CLAIMS = "Token payload missing required claims"
_MSG_USER_NOT_FOUND = "User not found"
_MSG_USER_DEACTIVATED = "User is deactivated"
_MSG_INVALID_API_KEY = "Invalid API key"
_MSG_NO_AUTH = "No authentication provided"
_MSG_ROLE_CHECK_FAILED = "Required role check failed"


# ── Exceptions ────────────────────────────────────────────────────────


def _unauthorized(detail: str = _MSG_MISSING_BEARER) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str = "Not authorised") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


# ── Current user (JWT) ───────────────────────────────────────────────


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> UserModel:
    """Extract and validate the current user from a JWT Bearer token.

    The token payload must contain ``sub`` (user id), ``tenant_id``,
    and ``role``.  A database lookup verifies the user still exists and
    is active.

    Raises 401 on missing / expired / invalid tokens.
    """
    if credentials is None:
        raise _unauthorized(_MSG_MISSING_BEARER)

    container = get_container(request)
    settings = container.settings

    try:
        payload: dict[str, Any] = decode_token(
            credentials.credentials, settings
        )
    except JWTError:
        raise _unauthorized(_MSG_INVALID_TOKEN) from None

    if payload.get("type") != "access":
        raise _unauthorized(_MSG_NOT_ACCESS_TOKEN)

    user_id: str | None = payload.get("sub")
    tenant_id: str | None = payload.get("tenant_id")
    if user_id is None or tenant_id is None:
        raise _unauthorized(_MSG_MISSING_CLAIMS)

    result = await db.execute(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.tenant_id == tenant_id,
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _unauthorized(_MSG_USER_NOT_FOUND)
    if not user.is_active:
        raise _unauthorized(_MSG_USER_DEACTIVATED)

    return user


# ── Current API key (alternative auth) ───────────────────────────────


class APIKeyAuthResult:
    """Holds the result of API key authentication."""

    def __init__(
        self,
        *,
        api_key: ApiKey | None = None,
        permissions: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.permissions = permissions
        self.tenant_id = tenant_id


async def get_current_api_key(
    request: Request,  # noqa: ARG001
    x_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> APIKeyAuthResult | None:
    """Authenticate via ``X-API-Key`` header.

    Returns ``None`` when no header is present (callers should fall
    back to JWT auth).  Raises 401 on invalid / inactive key.
    """
    if x_api_key is None:
        return None

    # Derive the key prefix (same logic as generation) and look up
    prefix = x_api_key[:8]
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.key_prefix == prefix,
            ApiKey.is_active == True,  # noqa: E712
        )
    )
    key_row = result.scalar_one_or_none()
    if key_row is None:
        raise _unauthorized(_MSG_INVALID_API_KEY)

    if not verify_api_key(x_api_key, key_row.key_hash):
        raise _unauthorized(_MSG_INVALID_API_KEY)

    # Update last_used_at (best-effort)
    key_row.last_used_at = datetime.now(UTC)  # type: ignore[misc]
    await db.flush()

    return APIKeyAuthResult(
        api_key=key_row,
        permissions=key_row.permissions,
        tenant_id=key_row.tenant_id,
    )


# ── Combined auth (try JWT first, fall back to API key) ──────────────


class AuthContext:
    """Unified authentication context with user or API key."""

    def __init__(
        self,
        *,
        user: UserModel | None = None,
        api_key_result: APIKeyAuthResult | None = None,
    ) -> None:
        self.user = user
        self.api_key_result = api_key_result

    @property
    def tenant_id(self) -> str:
        if self.user is not None:
            return str(self.user.tenant_id)
        if self.api_key_result is not None and self.api_key_result.tenant_id:
            return str(self.api_key_result.tenant_id)
        msg = "No authenticated principal"
        raise RuntimeError(msg)

    @property
    def principal_id(self) -> str:
        if self.user is not None:
            return str(self.user.id)
        if self.api_key_result is not None and self.api_key_result.api_key:
            return str(self.api_key_result.api_key.id)
        msg = "No authenticated principal"
        raise RuntimeError(msg)


async def get_auth_context(
    user: UserModel | None = Depends(get_current_user),
    api_key_result: APIKeyAuthResult | None = Depends(get_current_api_key),
) -> AuthContext:
    """Resolve ``AuthContext`` — prefers JWT user, falls back to API key.

    At least one authentication method must succeed.
    """
    if user is not None:
        return AuthContext(user=user)
    if api_key_result is not None:
        return AuthContext(api_key_result=api_key_result)
    raise _unauthorized(_MSG_NO_AUTH)


# ── Permission / role guards ─────────────────────────────────────────


def require_permission(resource: str, action: str) -> Any:
    """FastAPI dependency factory: guard a route by permission.

    Usage::

        @router.get("/transactions")
        async def list_transactions(
            auth: AuthContext = Depends(require_permission(
                "transactions", "read",
            )),
        ):
            ...

    Checks both JWT-authenticated users (by role) and API key principals
    (by key-level permission string).
    """
    _forbidden_msg = f"Missing required permission: {resource}:{action}"

    async def _perm_check(
        auth: AuthContext = Depends(get_auth_context),
    ) -> AuthContext:
        if auth.user is not None and user_has_permission(
            auth.user.role, resource, action
        ):
            return auth
        if auth.api_key_result is not None and api_key_has_permission(
            auth.api_key_result.permissions, resource, action
        ):
            return auth
        raise _forbidden(_forbidden_msg)

    return _perm_check


def require_role(*allowed_roles: str) -> Any:
    """FastAPI dependency factory: guard a route by user role.

    Only applies to JWT-authenticated users; API key principals that
    pass permission checks are also allowed.

    Usage::

        @router.delete("/api-keys/{id}")
        async def delete_api_key(
            auth: AuthContext = Depends(require_role("admin")),
        ):
            ...
    """
    _role_msg = f"Required one of roles {allowed_roles!r}, got '%s'"

    async def _role_check(
        auth: AuthContext = Depends(get_auth_context),
    ) -> AuthContext:
        if auth.user is not None:
            if auth.user.role in allowed_roles:
                return auth
            raise _forbidden(_role_msg % auth.user.role)
        # API key — pass if they have a permission check elsewhere, but
        # skip the role check since API keys don't have roles.
        if auth.api_key_result is not None:
            return auth
        raise _forbidden(_MSG_ROLE_CHECK_FAILED)

    return _role_check
