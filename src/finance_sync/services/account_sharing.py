"""Account sharing service — visibility transitions, claims, previews.

A user may only share or unshare **their own** accounts; tenant admins
may additionally claim (take ownership of) system-owned legacy accounts
so they can manage them.  Every transition is recorded in the
tenant-scoped household audit log with sanitised payloads.

``share_preview`` computes the impact of a visibility transition (how
many transactions, holdings and what balances would appear in or
disappear from the household view) so the UI can show it **before**
confirmation — there is no silent widening of the household picture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from finance_sync.models.account import Account
from finance_sync.models.balance import Balance
from finance_sync.models.enums import AccountVisibility, UserRole
from finance_sync.models.holding import Holding
from finance_sync.models.household_audit_log import (
    AUDIT_ACCOUNT_CLAIM,
    AUDIT_ACCOUNT_SHARE,
    AUDIT_ACCOUNT_UNSHARE,
    HouseholdAuditLog,
)
from finance_sync.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.models.user import User

HOUSEHOLD = AccountVisibility.HOUSEHOLD.value
PRIVATE = AccountVisibility.PRIVATE.value

# ── Stable error codes (used by the API layer) ───────────────────────

ERR_NOT_FOUND = "not_found"
ERR_FORBIDDEN = "forbidden"
ERR_INVALID_VISIBILITY = "invalid_visibility"


class AccountSharingError(Exception):
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
    actor: User,
) -> None:
    session.add(
        HouseholdAuditLog(
            tenant_id=tenant_id,
            action=action,
            detail=detail,
            actor_user_id=str(actor.id),
            actor_role=actor.role,
        )
    )


async def get_account_for_management(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    actor: User,
) -> Account:
    """Load an account the actor may manage, or raise.

    Management rights: the account owner, or an admin for system-owned
    (unclaimed) accounts.  A 404-equivalent error is raised for accounts
    the actor cannot manage so existence is not leaked.
    """
    result = await session.execute(
        select(Account).where(
            Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Account.id == account_id,  # type: ignore[attr-defined]
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        msg = "Account not found"
        raise AccountSharingError(ERR_NOT_FOUND, msg)
    if str(account.owner_user_id) == str(actor.id):
        return account
    if account.owner_user_id is None and actor.role == UserRole.ADMIN.value:
        return account
    msg = "Account not found"
    raise AccountSharingError(ERR_NOT_FOUND, msg)


async def set_account_visibility(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    visibility: str,
    actor: User,
) -> Account:
    """Set an account's visibility (owner-only; admin for unowned)."""
    if visibility not in {PRIVATE, HOUSEHOLD}:
        msg = (
            f"Invalid visibility {visibility!r}; expected 'private' "
            "or 'household'"
        )
        raise AccountSharingError(ERR_INVALID_VISIBILITY, msg)

    account = await get_account_for_management(
        session, tenant_id=tenant_id, account_id=account_id, actor=actor
    )

    old_visibility = account.visibility
    if old_visibility == visibility:
        return account

    account.visibility = visibility
    _audit(
        session,
        tenant_id=tenant_id,
        action=(
            AUDIT_ACCOUNT_SHARE
            if visibility == HOUSEHOLD
            else AUDIT_ACCOUNT_UNSHARE
        ),
        detail={
            "account_id": str(account.id),
            "account_name": account.name,
            "provider_key": account.provider_key,
            "old_visibility": old_visibility,
            "new_visibility": visibility,
        },
        actor=actor,
    )
    await session.flush()
    return account


async def claim_account(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    actor: User,
) -> Account:
    """Claim a system-owned account (admin only).

    Legacy accounts migrated with ``owner_user_id`` NULL are visible to
    admins; claiming assigns them to the acting admin so they can be
    shared or kept private under explicit ownership.
    """
    result = await session.execute(
        select(Account).where(
            Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Account.id == account_id,  # type: ignore[attr-defined]
        )
    )
    account = result.scalar_one_or_none()
    if account is None or account.owner_user_id is not None:
        msg = "Account not found or already owned"
        raise AccountSharingError(ERR_NOT_FOUND, msg)
    if actor.role != UserRole.ADMIN.value:
        msg = "Only admins can claim unowned accounts"
        raise AccountSharingError(ERR_FORBIDDEN, msg)

    account.owner_user_id = str(actor.id)
    _audit(
        session,
        tenant_id=tenant_id,
        action=AUDIT_ACCOUNT_CLAIM,
        detail={
            "account_id": str(account.id),
            "account_name": account.name,
            "provider_key": account.provider_key,
            "new_owner_user_id": str(actor.id),
        },
        actor=actor,
    )
    await session.flush()
    return account


# ── Share preview ─────────────────────────────────────────────────────


async def share_preview(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    actor: User,
) -> dict[str, Any]:
    """Compute the impact of sharing/unsharing an account.

    Returns counts of transactions, holdings, balance snapshots and the
    current balance — the data that would **appear** (private →
    household) or **disappear** (household → private) from the household
    view.  The UI renders this before asking for confirmation.
    """
    account = await get_account_for_management(
        session, tenant_id=tenant_id, account_id=account_id, actor=actor
    )

    txn_count = await _count(
        session,
        Transaction,
        Transaction.account_id == account_id,  # type: ignore[attr-defined]
    )
    holding_count = await _count(
        session,
        Holding,
        Holding.account_id == account_id,  # type: ignore[attr-defined]
    )
    balance_count = await _count(
        session,
        Balance,
        Balance.account_id == account_id,  # type: ignore[attr-defined]
    )

    return {
        "account_id": str(account.id),
        "account_name": account.name,
        "current_visibility": account.visibility,
        "target_visibility": (
            PRIVATE if account.visibility == HOUSEHOLD else HOUSEHOLD
        ),
        "impact": {
            "transactions": txn_count,
            "holdings": holding_count,
            "balance_snapshots": balance_count,
            "current_balance": (
                str(account.current_balance)
                if account.current_balance is not None
                else None
            ),
            "currency_code": account.currency_code,
        },
    }


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    result = await session.execute(
        select(func.count()).select_from(model).where(*where)
    )
    return int(result.scalar_one() or 0)
