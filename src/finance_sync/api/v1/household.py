"""Household management endpoints — invitations, members, audit log.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.

Endpoints
---------
* ``POST   /household/invitations``            — invite a new member (admin)
* ``GET    /household/invitations``            — list invitations (admin)
* ``POST   /household/invitations/accept``     — accept an invitation (public)
* ``POST   /household/invitations/{id}/revoke``— revoke an invitation (admin)
* ``GET    /household/members``                — list household members
* ``PATCH  /household/members/{user_id}/role`` — change a member's role (admin)
* ``DELETE /household/members/{user_id}``      — remove a member (admin)
* ``GET    /household/audit-log``              — security audit trail (admin)

The invite endpoint returns an identical generic response whether or not
the invited email already belongs to a tenant user, so it never leaks
user-directory information.
"""

from datetime import datetime
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_current_user,
    require_role,
)
from finance_sync.dependencies import get_container, get_db
from finance_sync.services.household import (
    HouseholdError,
    accept_invitation,
    change_member_role,
    create_invitation,
    list_audit_events,
    list_invitations,
    list_members,
    remove_member,
    revoke_invitation,
)

router = APIRouter(prefix="/household", tags=["household"])


# ── Schemas ───────────────────────────────────────────────────────────


class InviteRequest(BaseModel):
    email: str = Field(..., max_length=320, description="Invitee email")
    role: str = Field(
        default="user",
        description="Role to grant on acceptance: admin/user/readonly/viewer",
    )


class InviteResponse(BaseModel):
    id: str
    email: str
    role: str
    status: str
    expires_at: datetime
    token: str = Field(
        description=(
            "Single-use invitation token — shown exactly once; share it "
            "with the invitee out-of-band"
        )
    )


class InvitationSummary(BaseModel):
    id: str
    email: str
    role: str
    status: str
    expires_at: datetime
    created_by: str
    created_at: datetime


class AcceptInviteRequest(BaseModel):
    token: str = Field(..., min_length=16, max_length=256)
    email: str = Field(..., max_length=320)
    password: str = Field(..., min_length=8)
    display_name: str | None = Field(default=None, max_length=256)


class AcceptInviteResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: "UserResponse"


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    is_active: bool
    tenant_id: str


class RoleChangeRequest(BaseModel):
    role: str = Field(..., description="New role: admin/user/readonly/viewer")


class AuditEventResponse(BaseModel):
    id: str
    action: str
    detail: dict[str, Any]
    actor_user_id: str | None = None
    actor_role: str | None = None
    created_at: datetime


# ── Error mapping ─────────────────────────────────────────────────────


def _raise_household_error(exc: HouseholdError) -> NoReturn:
    code = exc.code
    if code == "invalid_token":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
        )
    if code == "email_taken":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.message
        )
    if code == "invalid_role":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        )
    if code in {"self_role_change", "self_removal", "last_admin"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.message
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
    )


# ── Invitations ───────────────────────────────────────────────────────


@router.post("/invitations", response_model=InviteResponse, status_code=201)
async def invite_member(
    body: InviteRequest,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> InviteResponse:
    """Invite a new household member (admin only).

    The response is identical whether or not *email* already belongs to
    a tenant user — no user-directory probing.  The raw ``token`` is
    returned exactly once.
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage household invitations",
        )
    try:
        invitation, raw_token = await create_invitation(
            db,
            tenant_id=auth.tenant_id,
            email=body.email,
            role=body.role,
            actor=auth.user,
        )
    except HouseholdError as exc:
        _raise_household_error(exc)
    await db.commit()
    return InviteResponse(
        id=str(invitation.id),
        email=invitation.email,
        role=invitation.role,
        status=invitation.status,
        expires_at=invitation.expires_at,
        token=raw_token,
    )


@router.get("/invitations", response_model=list[InvitationSummary])
async def get_invitations(
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> list[InvitationSummary]:
    """List the tenant's household invitations (admin only)."""
    invitations = await list_invitations(db, tenant_id=auth.tenant_id)
    return [
        InvitationSummary(
            id=str(inv.id),
            email=inv.email,
            role=inv.role,
            status=inv.status,
            expires_at=inv.expires_at,
            created_by=inv.created_by,
            created_at=inv.created_at,
        )
        for inv in invitations
    ]


@router.post("/invitations/accept", response_model=AcceptInviteResponse)
async def accept_invite(
    body: AcceptInviteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AcceptInviteResponse:
    """Accept a household invitation and log the new member in.

    Public endpoint — no auth required (the invitee has no account yet).
    The token is single-use; a second call fails.
    """
    container = get_container(request)
    try:
        user, access_token, refresh_token = await accept_invitation(
            db,
            token=body.token,
            email=body.email,
            password=body.password,
            display_name=body.display_name,
            settings=container.settings,
        )
    except HouseholdError as exc:
        _raise_household_error(exc)
    return AcceptInviteResponse(
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


@router.post("/invitations/{invitation_id}/revoke", status_code=204)
async def revoke_invite(
    invitation_id: str,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a pending invitation (admin only)."""
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage household invitations",
        )
    revoked = await revoke_invitation(
        db,
        tenant_id=auth.tenant_id,
        invitation_id=invitation_id,
        actor=auth.user,
    )
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending invitation not found",
        )
    await db.commit()


# ── Members ───────────────────────────────────────────────────────────


@router.get("/members", response_model=list[UserResponse])
async def get_members(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserResponse]:
    """List the household members (any authenticated member)."""
    members = await list_members(db, tenant_id=auth.tenant_id)
    return [
        UserResponse(
            id=str(m.id),
            email=m.email,
            display_name=m.display_name,
            role=m.role,
            is_active=m.is_active,
            tenant_id=str(m.tenant_id),
        )
        for m in members
    ]


@router.patch("/members/{member_id}/role", response_model=UserResponse)
async def update_member_role(
    member_id: str,
    body: RoleChangeRequest,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Change a member's role (admin only)."""
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage household members",
        )
    try:
        member = await change_member_role(
            db,
            tenant_id=auth.tenant_id,
            member_id=member_id,
            new_role=body.role,
            actor=auth.user,
        )
    except HouseholdError as exc:
        _raise_household_error(exc)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found",
        )
    await db.commit()
    return UserResponse(
        id=str(member.id),
        email=member.email,
        display_name=member.display_name,
        role=member.role,
        is_active=member.is_active,
        tenant_id=str(member.tenant_id),
    )


@router.delete("/members/{member_id}", status_code=204)
async def delete_member(
    member_id: str,
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a member from the household (admin only).

    The removed member's private accounts become system-owned so tenant
    admins can still see, claim and reassign them — nothing is silently
    deleted.
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage household members",
        )
    try:
        removed = await remove_member(
            db,
            tenant_id=auth.tenant_id,
            member_id=member_id,
            actor=auth.user,
        )
    except HouseholdError as exc:
        _raise_household_error(exc)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found",
        )
    await db.commit()


# ── Audit log ─────────────────────────────────────────────────────────


@router.get("/audit-log", response_model=list[AuditEventResponse])
async def get_audit_log(
    auth: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEventResponse]:
    """Return the tenant's household security audit trail (admin only)."""
    events = await list_audit_events(db, tenant_id=auth.tenant_id, limit=limit)
    return [
        AuditEventResponse(
            id=str(e.id),
            action=e.action,
            detail=e.detail or {},
            actor_user_id=e.actor_user_id,
            actor_role=e.actor_role,
            created_at=e.created_at,
        )
        for e in events
    ]
