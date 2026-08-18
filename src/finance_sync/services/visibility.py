"""Account read scope — single-owner enforcement core.

The application is a personal datalake with exactly one local owner per
installation.  Every read path (read API, derived services, exporter,
webhooks, MCP) applies the same scope policy:

* A JWT user (the tenant's owner) may read every account in their tenant.
* A machine principal (API key) may only read the accounts in its
  ``account_scope`` allowlist (e.g. a least-privilege Jupyter consumer
  key).  An unscoped API key reads the whole tenant datalake.

The tenant boundary itself remains as a technical isolation boundary;
it is not a household/sharing concept.

The module exposes both a SQL-level predicate (so queries stay in the
database) and an in-memory predicate (for post-query filtering where a
SQL join is impractical).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, select

from finance_sync.models.account import Account
from finance_sync.models.enums import UserRole

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.models.user import User


@dataclass(frozen=True)
class ReadScope:
    """Which accounts a principal may read within a tenant.

    * ``user_id`` set  → the owner: every account in the tenant.
    * ``user_id`` None → machine scope (API keys): only ``account_ids``
      when the key carries an explicit account scope, otherwise the whole
      tenant datalake.

    Pass an instance to ``ReadService`` / derived services to enforce the
    scope policy on every read.
    """

    tenant_id: str
    user_id: str | None = None
    is_admin: bool = False
    account_ids: frozenset[str] | None = None

    @classmethod
    def for_user(cls, user: User) -> ReadScope:
        """Build the scope for a JWT user."""
        return cls(
            tenant_id=str(user.tenant_id),
            user_id=str(user.id),
            is_admin=user.role == UserRole.ADMIN.value,
        )

    @classmethod
    def for_api_key(
        cls, tenant_id: str, account_scope: list[str] | None = None
    ) -> ReadScope:
        """Build the machine scope for an API-key principal.

        Machine principals read only the accounts in their scope
        allowlist when one is set (least privilege); unscoped keys read
        the tenant's whole datalake.
        """
        return cls(
            tenant_id=tenant_id,
            account_ids=(
                frozenset(str(account_id) for account_id in account_scope)
                if account_scope is not None
                else None
            ),
        )

    def account_filter(self) -> Any:
        """Return the SQL predicate over ``Account`` rows."""
        return account_visibility_where(
            self.tenant_id, account_ids=self.account_ids
        )

    def account_ids_subquery(self) -> Any:
        """Return a scalar subquery of visible account ids."""
        return visible_account_ids_subquery(
            self.tenant_id, account_ids=self.account_ids
        )

    def is_visible(self, account: Account) -> bool:
        """In-memory visibility check."""
        return account_is_visible(
            account,
            self.tenant_id,
            account_ids=self.account_ids,
        )


def account_visibility_where(
    tenant_id: str,
    *,
    account_ids: frozenset[str] | None = None,
) -> Any:
    """Return a SQLAlchemy predicate over ``Account`` rows.

    A user principal (the sole owner) reads every account in the tenant.
    A machine principal is restricted to ``account_ids`` when the caller
    supplies an explicit scope.
    """
    if account_ids is not None:
        return and_(
            Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Account.id.in_(account_ids),  # type: ignore[attr-defined]
        )
    return Account.tenant_id == tenant_id  # type: ignore[attr-defined]


def visible_account_ids_subquery(
    tenant_id: str,
    *,
    account_ids: frozenset[str] | None = None,
) -> Any:
    """Return a scalar subquery of the visible account ids for *tenant_id*.

    Use it as ``SomeModel.account_id.in_(subquery)`` to scope derived
    tables (transactions, holdings, balances, …) without loading ids
    into Python.
    """
    return select(Account.id).where(
        account_visibility_where(
            tenant_id,
            account_ids=account_ids,
        )
    )


async def load_visible_account_ids(
    session: AsyncSession,
    tenant_id: str,
    *,
    account_ids: frozenset[str] | None = None,
) -> set[str]:
    """Return the set of visible account ids for the principal."""
    stmt = visible_account_ids_subquery(
        tenant_id,
        account_ids=account_ids,
    )
    rows = await session.scalars(stmt)
    return {str(account_id) for account_id in rows}


def account_is_visible(
    account: Account,
    tenant_id: str,
    *,
    account_ids: frozenset[str] | None = None,
) -> bool:
    """Return whether *account* is visible to the principal.

    In-memory counterpart of :func:`account_visibility_where`.
    """
    if str(account.tenant_id) != tenant_id:
        return False
    if account_ids is not None:
        return str(account.id) in account_ids
    return True


def filter_accounts_visible(
    accounts: Sequence[Account],
    tenant_id: str,
    *,
    account_ids: frozenset[str] | None = None,
) -> list[Account]:
    """Filter an in-memory account list by the scope policy."""
    return [
        account
        for account in accounts
        if account_is_visible(
            account,
            tenant_id,
            account_ids=account_ids,
        )
    ]


def scope_from_user(user: User) -> tuple[str, bool]:
    """Return ``(user_id, is_admin)`` for a JWT user."""
    return str(user.id), user.role == UserRole.ADMIN
