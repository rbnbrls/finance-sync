"""Account ingestion stage for the sync pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from finance_sync.connectors.base import Connector
    from finance_sync.connectors.models import CanonicalAccountData
    from finance_sync.db.uow import UnitOfWork


class AccountStageWriter(Protocol):
    """Persistence boundary required by the account stage."""

    async def persist_account(
        self,
        uow: UnitOfWork,
        account: CanonicalAccountData,
        *,
        connection_id: str | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class AccountStageResult:
    """Facts produced by account ingestion."""

    accounts: list[CanonicalAccountData]
    supports_holdings: bool


class AccountSyncStage:
    """Fetch, filter and persist canonical accounts without committing."""

    def __init__(self, writer: AccountStageWriter) -> None:
        self._writer = writer

    async def run(
        self,
        uow: UnitOfWork,
        connector: Connector,
        *,
        selected_accounts: list[str] | None = None,
        connection_id: str | None = None,
        persist: bool = True,
    ) -> AccountStageResult:
        """Fetch accounts and optionally persist them in the caller's UoW."""
        raw_accounts = await connector._rate_limited_fetch_accounts()  # type: ignore[attr-defined]
        accounts = connector.transform_accounts(raw_accounts)
        selected = set(selected_accounts) if selected_accounts else None
        if selected is not None:
            accounts = [
                account
                for account in accounts
                if account.external_account_id in selected
            ]
        if persist:
            for account in accounts:
                await self._writer.persist_account(
                    uow, account, connection_id=connection_id
                )
        resources = cast(
            "frozenset[str]",
            getattr(type(connector), "supported_resources", frozenset[str]()),
        )
        return AccountStageResult(
            accounts=accounts,
            supports_holdings="holdings" in resources,
        )
