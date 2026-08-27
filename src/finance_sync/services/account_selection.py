"""Account selection helpers for multi-connection sync/export filtering.

A connection (credential row) may pin the provider accounts it wants
synced and exported via ``selected_accounts``.  These helpers translate
that per-connection selection into a predicate over local ``Account``
rows so the sync pipeline and the Wealthfolio exporter both honour it:

* an account whose ``connection_id`` is NULL (pre-multi-connection
  legacy rows) is always kept — dropping it would silently delete
  history that predates account selection;
* an account whose connection row no longer exists (e.g. the connection
  was deleted without purging data) is kept as well — deletion of
  history is an explicit, confirmed operation elsewhere;
* an account of a connection with ``selected_accounts`` set is kept only
  when its external account id is in the selection;
* an account of a connection without a selection (NULL/empty) is kept
  (the connection syncs everything).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from finance_sync.models import Account, Credential

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: connection_id -> selected external account ids, or None = all accounts
AccountSelection = dict[str, set[str] | None]


async def load_account_selection(
    session: AsyncSession,
    tenant_id: str,
) -> AccountSelection:
    """Return the per-connection account selection for *tenant_id*.

    The map is keyed by connection id; ``None`` means the connection
    selects all accounts the provider offers.
    """
    rows = await session.scalars(
        select(Credential).where(Credential.tenant_id == tenant_id)
    )
    selection: AccountSelection = {}
    for cred in rows:
        selected = cred.selected_accounts
        selection[str(cred.id)] = set(selected) if selected else None
    return selection


def account_is_selected(
    account: Account,
    selection: AccountSelection,
) -> bool:
    """Return whether *account* may be synced/exported.

    See the module docstring for the exact rules; the short version is:
    legacy rows and orphaned rows are kept, selected connections are
    restricted to their selection, unselected connections keep all.
    """
    connection_id = account.connection_id
    if connection_id is None:
        # Legacy row imported before account selection existed.
        return True
    selected = selection.get(str(connection_id))
    if not selected:
        # Connection without a selection (or an empty selection, or the
        # connection row is gone): keep the account — history is only
        # removed by explicit purge.
        return True
    return account.external_account_id in selected


async def filter_accounts(
    session: AsyncSession,
    tenant_id: str,
    accounts: list[Account],
) -> list[Account]:
    """Filter *accounts* by the tenant's per-connection selection."""
    if not accounts:
        return accounts
    selection = await load_account_selection(session, tenant_id)
    return [a for a in accounts if account_is_selected(a, selection)]


async def filter_account_ids(
    session: AsyncSession,
    tenant_id: str,
    account_ids: list[str],
) -> list[str]:
    """Filter local account ids by the tenant's per-connection selection.

    Used by callers that already hold the account rows' selection state;
    loads the accounts by id and applies the same predicate.
    """
    if not account_ids:
        return account_ids
    rows = await session.scalars(
        select(Account).where(
            Account.tenant_id == tenant_id,
            Account.id.in_(account_ids),
        )
    )
    by_id: dict[str, Account] = {str(a.id): a for a in rows}
    selection = await load_account_selection(session, tenant_id)
    return [
        account_id
        for account_id in account_ids
        if (account := by_id.get(account_id)) is not None
        and account_is_selected(account, selection)
    ]
