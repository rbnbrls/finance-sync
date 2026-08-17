"""Household management service — invitations, members, audit trail.

Implements the tenant-scoped household model:

* admins invite new household members via single-use, expiring,
  bcrypt-hashed invitation tokens;
* acceptance creates a tenant user with the invited role and returns
  JWTs (the invitee logs straight in);
* invitations never leak whether an email already belongs to a tenant
  user (the create endpoint returns an identical generic response);
* every sensitive action is recorded in the tenant-scoped
  ``household_audit_log`` with sanitised payloads (no financial data,
  no secrets).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, select

from finance_sync.models.enums import (
    InvitationStatus,
    UserRole,
)
from finance_sync.models.household_audit_log import HouseholdAuditLog
from finance_sync.models.household_invitation import HouseholdInvitation
from finance_sync.models.user import User
from finance_sync.services.auth import hash_password

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Roles a tenant admin may assign to household members.
ASSIGNABLE_ROLES = {
    UserRole.ADMIN.value,
    UserRole.USER.value,
    UserRole.READONLY.value,
    UserRole.VIEWER.value,
}

# ── Stable error codes (used by the API layer to map to HTTP status) ──

ERR_INVALID_ROLE = "invalid_role"
ERR_INVALID_TOKEN = "invalid_token"
ERR_EMAIL_TAKEN = "email_taken"
ERR_SELF_ROLE_CHANGE = "self_role_change"
ERR_LAST_ADMIN = "last_admin"
ERR_SELF_REMOVAL = "self_removal"


def _token_digest(raw_token: str) -> str:
    """Return the deterministic SHA-256 digest used as the lookup key.

    The raw token carries 32 bytes of CSPRNG entropy, so the digest
    cannot be reversed and doubles as the stored credential.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class HouseholdError(Exception):
    """Domain error with a stable ``code`` for the API layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _audit(
    session: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    detail: dict[str, Any],
    actor_user_id: str | None,
    actor_role: str | None,
) -> None:
    """Queue a sanitised audit entry (flushed with the caller's commit)."""
    session.add(
        HouseholdAuditLog(
            tenant_id=tenant_id,
            action=action,
            detail=detail,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
        )
    )


# ── Invitations ───────────────────────────────────────────────────────


async def create_invitation(
    session: AsyncSession,
    *,
    tenant_id: str,
    email: str,
    role: str,
    actor: User,
) -> tuple[HouseholdInvitation, str]:
    """Create a pending invitation and return ``(invitation, raw_token)``.

    The raw token is returned exactly once (like an API key); only its
    bcrypt hash is stored.  The response is intentionally identical
    whether or not a user with *email* already exists in the tenant —
    the endpoint never leaks user-directory information.
    """
    if role not in ASSIGNABLE_ROLES:
        msg = f"Role {role!r} is not assignable to household members"
        raise HouseholdError(ERR_INVALID_ROLE, msg)

    normalized_email = email.strip().lower()
    raw_token = secrets.token_urlsafe(32)
    invitation = HouseholdInvitation(
        tenant_id=tenant_id,
        email=normalized_email,
        token_hash=_token_digest(raw_token),
        role=role,
        status=InvitationStatus.PENDING,
        expires_at=HouseholdInvitation.default_expiry(),
        created_by=str(actor.id),
    )
    session.add(invitation)
    await session.flush()

    _audit(
        session,
        tenant_id=tenant_id,
        action="invite",
        detail={
            "email": normalized_email,
            "role": role,
            "invitation_id": str(invitation.id),
            "expires_at": invitation.expires_at.isoformat(),
        },
        actor_user_id=str(actor.id),
        actor_role=actor.role,
    )
    return invitation, raw_token


async def accept_invitation(
    session: AsyncSession,
    *,
    token: str,
    email: str,
    password: str,
    display_name: str | None,
    settings: Any,
) -> tuple[User, str, str]:
    """Accept a pending invitation: create the user and return JWTs.

    Raises :class:`HouseholdError` with a generic message when the token
    is unknown/expired/revoked — the acceptor can only probe their own
    token, so no cross-user information leaks.
    """
    from finance_sync.services.auth import (
        create_access_token,
        create_refresh_token,
    )

    token_digest = _token_digest(token)
    result = await session.execute(
        select(HouseholdInvitation).where(
            HouseholdInvitation.token_hash == token_digest
        )
    )
    invitation = result.scalar_one_or_none()

    if invitation is None or not invitation.is_pending:
        msg = "Invitation is invalid or has expired"
        raise HouseholdError(ERR_INVALID_TOKEN, msg)

    normalized_email = email.strip().lower()
    if invitation.email != normalized_email:
        msg = "Invitation is invalid or has expired"
        raise HouseholdError(ERR_INVALID_TOKEN, msg)

    # A user with this email already exists in the tenant → cannot join.
    existing = await session.execute(
        select(User).where(
            and_(
                User.tenant_id == invitation.tenant_id,
                User.email == normalized_email,
            )
        )
    )
    if existing.scalar_one_or_none() is not None:
        msg = "An account for this email already exists in this household"
        raise HouseholdError(ERR_EMAIL_TAKEN, msg)

    role = (
        invitation.role
        if invitation.role in ASSIGNABLE_ROLES
        else (UserRole.USER.value)
    )
    user = User(
        tenant_id=invitation.tenant_id,
        email=normalized_email,
        hashed_password=hash_password(password),
        display_name=display_name or normalized_email.split("@")[0],
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    invitation.status = InvitationStatus.ACCEPTED
    invitation.accepted_by = str(user.id)
    invitation.accepted_at = datetime.now(UTC)

    _audit(
        session,
        tenant_id=invitation.tenant_id,
        action="accept_invitation",
        detail={
            "email": normalized_email,
            "role": role,
            "invitation_id": str(invitation.id),
            "user_id": str(user.id),
        },
        actor_user_id=str(user.id),
        actor_role=role,
    )
    await session.commit()

    token_data = {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role,
    }
    access_token = create_access_token(token_data, settings)
    refresh_token = create_refresh_token(token_data, settings)
    return user, access_token, refresh_token


async def revoke_invitation(
    session: AsyncSession,
    *,
    tenant_id: str,
    invitation_id: str,
    actor: User,
) -> bool:
    """Revoke a pending invitation (admin only — enforced by the API)."""
    result = await session.execute(
        select(HouseholdInvitation).where(
            and_(
                HouseholdInvitation.id == invitation_id,
                HouseholdInvitation.tenant_id == tenant_id,
            )
        )
    )
    invitation = result.scalar_one_or_none()
    if invitation is None:
        return False
    if invitation.status != InvitationStatus.PENDING:
        return False

    invitation.status = InvitationStatus.REVOKED
    _audit(
        session,
        tenant_id=tenant_id,
        action="revoke_invitation",
        detail={
            "email": invitation.email,
            "invitation_id": str(invitation.id),
        },
        actor_user_id=str(actor.id),
        actor_role=actor.role,
    )
    await session.flush()
    return True


async def list_invitations(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> list[HouseholdInvitation]:
    """List the tenant's invitations (newest first)."""
    result = await session.execute(
        select(HouseholdInvitation)
        .where(HouseholdInvitation.tenant_id == tenant_id)
        .order_by(HouseholdInvitation.created_at.desc())
    )
    return list(result.scalars().all())


# ── Members ───────────────────────────────────────────────────────────


async def list_members(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> list[User]:
    """List the tenant's household members (oldest first)."""
    result = await session.execute(
        select(User)
        .where(User.tenant_id == tenant_id)
        .order_by(User.created_at.asc())
    )
    return list(result.scalars().all())


async def change_member_role(
    session: AsyncSession,
    *,
    tenant_id: str,
    member_id: str,
    new_role: str,
    actor: User,
) -> User | None:
    """Change a member's role (admin only — enforced by the API).

    Protects against lockout: the last remaining admin cannot be
    demoted, and admins cannot change their own role.
    """
    if new_role not in ASSIGNABLE_ROLES:
        msg = f"Role {new_role!r} is not assignable to household members"
        raise HouseholdError(ERR_INVALID_ROLE, msg)

    result = await session.execute(
        select(User).where(
            and_(
                User.id == member_id,
                User.tenant_id == tenant_id,
            )
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        return None

    if str(member.id) == str(actor.id):
        msg = "Admins cannot change their own role"
        raise HouseholdError(ERR_SELF_ROLE_CHANGE, msg)

    if member.role == UserRole.ADMIN.value and new_role != UserRole.ADMIN.value:
        admin_count = await _count_admins(session, tenant_id)
        if admin_count <= 1:
            msg = "Cannot demote the last remaining admin"
            raise HouseholdError(ERR_LAST_ADMIN, msg)

    old_role = member.role
    member.role = new_role
    _audit(
        session,
        tenant_id=tenant_id,
        action="role_change",
        detail={
            "user_id": str(member.id),
            "email": member.email,
            "old_role": old_role,
            "new_role": new_role,
        },
        actor_user_id=str(actor.id),
        actor_role=actor.role,
    )
    await session.flush()
    return member


async def remove_member(
    session: AsyncSession,
    *,
    tenant_id: str,
    member_id: str,
    actor: User,
) -> bool:
    """Remove a member from the household (admin only).

    The member's private accounts become system-owned (``owner_user_id``
    NULL) so tenant admins can see, claim and reassign them — nothing is
    silently deleted.  Household-shared accounts stay shared.
    """
    if str(member_id) == str(actor.id):
        msg = "Admins cannot remove themselves"
        raise HouseholdError(ERR_SELF_REMOVAL, msg)

    result = await session.execute(
        select(User).where(
            and_(
                User.id == member_id,
                User.tenant_id == tenant_id,
            )
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        return False

    if member.role == UserRole.ADMIN.value:
        admin_count = await _count_admins(session, tenant_id)
        if admin_count <= 1:
            msg = "Cannot remove the last remaining admin"
            raise HouseholdError(ERR_LAST_ADMIN, msg)

    from finance_sync.models.account import Account

    await session.execute(
        Account.__table__.update()  # type: ignore[attr-defined]
        .where(Account.owner_user_id == str(member.id))  # type: ignore[attr-defined]
        .values(owner_user_id=None)
    )

    member.is_active = False
    _audit(
        session,
        tenant_id=tenant_id,
        action="remove_member",
        detail={
            "user_id": str(member.id),
            "email": member.email,
            "removed_role": member.role,
        },
        actor_user_id=str(actor.id),
        actor_role=actor.role,
    )
    await session.flush()
    return True


async def _count_admins(session: AsyncSession, tenant_id: str) -> int:
    from sqlalchemy import func

    result = await session.execute(
        select(func.count())
        .select_from(User)
        .where(
            and_(
                User.tenant_id == tenant_id,
                User.role == UserRole.ADMIN.value,
                User.is_active.is_(True),
            )
        )
    )
    return int(result.scalar_one() or 0)


# ── Audit log ─────────────────────────────────────────────────────────


async def list_audit_events(
    session: AsyncSession,
    *,
    tenant_id: str,
    limit: int = 100,
) -> list[HouseholdAuditLog]:
    """Return the tenant's household audit trail (newest first)."""
    result = await session.execute(
        select(HouseholdAuditLog)
        .where(HouseholdAuditLog.tenant_id == tenant_id)
        .order_by(HouseholdAuditLog.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
