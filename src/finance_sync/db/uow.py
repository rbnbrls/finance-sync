"""Unit of Work pattern for finance-sync.

Wraps an ``AsyncSession`` in a context manager that commits on success
and rolls back on error.  Repositories are accessed as attributes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from finance_sync.db.repositories import (
    AccountRepository,
    BalanceRepository,
    EnrichmentFreshnessRepository,
    HoldingRepository,
    OutboxMessageRepository,
    ResolutionAuditLogRepository,
    SecurityListingRepository,
    SecurityPriceRepository,
    SecurityRepository,
    SyncRunRepository,
    TenantRepository,
    TransactionRepository,
    UnresolvedSecurityRepository,
    UserRepository,
)

if TYPE_CHECKING:
    from types import TracebackType

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.db.repository import Repository


class UnitOfWork:
    """Async context manager that provides a transactional boundary.

    Repositories are exposed as lazy-loaded attributes:

        async with UnitOfWork(session) as uow:
            account = await uow.accounts.get(account_id)

    On success the transaction is committed automatically; on exception
    it is rolled back.  Call ``commit()`` explicitly to flush mid-block.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repositories: dict[str, Repository] = {}
        self._committed = False

    @property
    def session(self) -> AsyncSession:
        return self._session

    # ── Repository accessors ─────────────────────────────────────────

    @property
    def tenants(self) -> TenantRepository:
        return self._repo("tenants", TenantRepository)  # type: ignore[return-value]

    @property
    def users(self) -> UserRepository:
        return self._repo("users", UserRepository)  # type: ignore[return-value]

    @property
    def accounts(self) -> AccountRepository:
        return self._repo("accounts", AccountRepository)  # type: ignore[return-value]

    @property
    def securities(self) -> SecurityRepository:
        return self._repo("securities", SecurityRepository)  # type: ignore[return-value]

    @property
    def security_listings(self) -> SecurityListingRepository:
        return self._repo("security_listings", SecurityListingRepository)  # type: ignore[return-value]

    @property
    def security_prices(self) -> SecurityPriceRepository:
        return self._repo("security_prices", SecurityPriceRepository)  # type: ignore[return-value]

    @property
    def enrichment_freshness(self) -> EnrichmentFreshnessRepository:
        return self._repo("enrichment_freshness", EnrichmentFreshnessRepository)  # type: ignore[return-value]

    @property
    def transactions(self) -> TransactionRepository:
        return self._repo("transactions", TransactionRepository)  # type: ignore[return-value]

    @property
    def holdings(self) -> HoldingRepository:
        return self._repo("holdings", HoldingRepository)  # type: ignore[return-value]

    @property
    def balances(self) -> BalanceRepository:
        return self._repo("balances", BalanceRepository)  # type: ignore[return-value]

    @property
    def outbox(self) -> OutboxMessageRepository:
        return self._repo("outbox", OutboxMessageRepository)  # type: ignore[return-value]

    @property
    def sync_runs(self) -> SyncRunRepository:
        return self._repo("sync_runs", SyncRunRepository)  # type: ignore[return-value]

    @property
    def unresolved_securities(self) -> UnresolvedSecurityRepository:
        return self._repo("unresolved_securities", UnresolvedSecurityRepository)  # type: ignore[return-value]

    @property
    def resolution_audit_log(self) -> ResolutionAuditLogRepository:
        return self._repo("resolution_audit_log", ResolutionAuditLogRepository)  # type: ignore[return-value]

    # ── Lifecycle ────────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None and not self._committed:
            await self._session.commit()
        elif exc_type is not None:
            await self._session.rollback()

    async def commit(self) -> None:
        """Explicitly commit the current transaction."""
        await self._session.commit()
        self._committed = True

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._session.rollback()

    # ── Internal ─────────────────────────────────────────────────────

    def _repo(self, key: str, repo_cls: type[Repository]) -> Repository:
        """Lazy-load a repository instance, caching it on first access."""
        if key not in self._repositories:
            self._repositories[key] = repo_cls(self._session)
        return self._repositories[key]
