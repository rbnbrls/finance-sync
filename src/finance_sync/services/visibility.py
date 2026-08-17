"""Account visibility — the enforcement core of household sharing.

Every read path (read API, derived services, exporter, webhooks, MCP)
must apply the same visibility policy.  A principal may read an account
when **any** of the following holds:

* the account is ``household``-visible (explicitly shared with the
  tenant), or
* the account's ``owner_user_id`` is the principal's user id (private
  accounts are only visible to their owner), or
* the account is system-owned (``owner_user_id`` NULL) **and** the
  principal is a tenant admin (admins see legacy/unclaimed accounts).

Machine principals (API keys) have no user id; they see only
``household`` and system-owned accounts — never the private accounts of
a specific user.

The module deliberately exposes both a SQL-level predicate (so queries
stay in the database) and an in-memory predicate (for post-query
filtering where a SQL join is impractical).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select

from finance_sync.models.account import Account
from finance_sync.models.enums import AccountVisibility, UserRole

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.models.user import User

#: Column value stored for household-shared accounts.
HOUSEHOLD = AccountVisibility.HOUSEHOLD.value
PRIVATE = AccountVisibility.PRIVATE.value


@dataclass(frozen=True)
class ReadScope:
    """Which accounts a principal may read within a tenant.

    * ``user_id`` set  → user scope: household OR own OR (admin AND
      unowned).
    * ``user_id`` None → machine scope (API keys): household OR unowned.

    Pass an instance to ``ReadService`` / derived services to enforce the
    household visibility policy on every read.
    """

    tenant_id: str
    user_id: str | None = None
    is_admin: bool = False

    @classmethod
    def for_user(cls, user: User) -> ReadScope:
        """Build the scope for a JWT user."""
        return cls(
            tenant_id=str(user.tenant_id),
            user_id=str(user.id),
            is_admin=user.role == UserRole.ADMIN.value,
        )

    @classmethod
    def for_api_key(cls, tenant_id: str) -> ReadScope:
        """Build the machine scope for an API-key principal.

        Machine principals (``user_id`` None) see only household and
        system-owned accounts — never the private accounts of a user.
        """
        return cls(tenant_id=tenant_id)

    def account_filter(self) -> Any:
        """Return the SQL predicate over ``Account`` rows."""
        return account_visibility_where(
            self.tenant_id,
            user_id=self.user_id,
            is_admin=self.is_admin,
        )

    def account_ids_subquery(self) -> Any:
        """Return a scalar subquery of visible account ids."""
        return visible_account_ids_subquery(
            self.tenant_id,
            user_id=self.user_id,
            is_admin=self.is_admin,
        )

    def is_visible(self, account: Account) -> bool:
        """In-memory visibility check."""
        return account_is_visible(
            account,
            self.tenant_id,
            user_id=self.user_id,
            is_admin=self.is_admin,
        )


def account_visibility_where(
    tenant_id: str,
    *,
    user_id: str | None = None,
    is_admin: bool = False,
) -> Any:
    """Return a SQLAlchemy predicate over ``Account`` rows.

    ``user_id`` set  → user scope: household OR own OR (admin AND unowned).
    ``user_id`` None → machine scope: household OR unowned.
    """
    tenant_cond = Account.tenant_id == tenant_id  # type: ignore[attr-defined]
    if user_id is not None:
        visible = or_(
            Account.visibility == HOUSEHOLD,  # type: ignore[attr-defined]
            Account.owner_user_id == user_id,  # type: ignore[attr-defined]
            and_(
                Account.owner_user_id.is_(None),  # type: ignore[attr-defined]
                is_admin,
            ),
        )
    else:
        visible = or_(
            Account.visibility == HOUSEHOLD,  # type: ignore[attr-defined]
            Account.owner_user_id.is_(None),  # type: ignore[attr-defined]
        )
    return and_(tenant_cond, visible)


def visible_account_ids_subquery(
    tenant_id: str,
    *,
    user_id: str | None = None,
    is_admin: bool = False,
) -> Any:
    """Return a scalar subquery of the visible account ids for *tenant_id*.

    Use it as ``SomeModel.account_id.in_(subquery)`` to scope derived
    tables (transactions, holdings, balances, …) without loading ids
    into Python.
    """
    return select(Account.id).where(
        account_visibility_where(
            tenant_id,
            user_id=user_id,
            is_admin=is_admin,
        )
    )


async def load_visible_account_ids(
    session: AsyncSession,
    tenant_id: str,
    *,
    user_id: str | None = None,
    is_admin: bool = False,
) -> set[str]:
    """Return the set of visible account ids for the principal."""
    stmt = visible_account_ids_subquery(
        tenant_id,
        user_id=user_id,
        is_admin=is_admin,
    )
    rows = await session.scalars(stmt)
    return {str(account_id) for account_id in rows}


def account_is_visible(
    account: Account,
    tenant_id: str,
    *,
    user_id: str | None = None,
    is_admin: bool = False,
) -> bool:
    """Return whether *account* is visible to the principal.

    In-memory counterpart of :func:`account_visibility_where`.
    """
    if str(account.tenant_id) != tenant_id:
        return False
    if account.visibility == HOUSEHOLD:
        return True
    if user_id is not None:
        if account.owner_user_id == user_id:
            return True
        return bool(account.owner_user_id is None and is_admin)
    # Machine scope (API key): household OR system-owned.
    return account.owner_user_id is None


def filter_accounts_visible(
    accounts: Sequence[Account],
    tenant_id: str,
    *,
    user_id: str | None = None,
    is_admin: bool = False,
) -> list[Account]:
    """Filter an in-memory account list by the visibility policy."""
    return [
        account
        for account in accounts
        if account_is_visible(
            account,
            tenant_id,
            user_id=user_id,
            is_admin=is_admin,
        )
    ]


def scope_from_user(user: User) -> tuple[str, bool]:
    """Return ``(user_id, is_admin)`` for a JWT user."""
    return str(user.id), user.role == UserRole.ADMIN
