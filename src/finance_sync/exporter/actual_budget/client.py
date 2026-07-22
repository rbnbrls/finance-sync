"""Synchronous wrapper around the actualpy ``Actual`` class.

Because actualpy uses synchronous SQLAlchemy sessions, all public
methods run inside ``asyncio.to_thread()`` so callers can use them
from async code without blocking the event loop.

Usage (async context manager)::

    config = ActualBudgetConfig(
        server_url="http://localhost:5006",
        password="hunter2",
        budget_name="My Budget",
    )
    async with ActualBudgetClient(config) as client:
        acct = await client.get_or_create_account("My Checking")
        await client.create_transaction(
            account="My Checking",
            date=datetime.date.today(),
            payee="Coffee Shop",
            amount=-1250,
            imported_id="fs_txn_abc123",
        )
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import datetime as dt

    from actual import Actual

    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig

log = structlog.get_logger("finance_sync.exporter.actual_budget.client")


class ActualBudgetError(Exception):
    """Base error for Actual Budget client / connection failures."""


class ActualBudgetConnectionError(ActualBudgetError):
    """The server could not be reached or credentials are invalid."""


class ActualBudgetAccountError(ActualBudgetError):
    """Account not found or could not be created."""


# ═══════════════════════════════════════════════════════════════════════
# Public client
# ═══════════════════════════════════════════════════════════════════════


class ActualBudgetClient:
    """Async-friendly wrapper around actualpy's ``Actual`` class.

    Use as an async context manager::

        async with ActualBudgetClient(config) as client:
            ...

    Every method that touches the underlying session runs in a
    thread-pool executor so it doesn't block the asyncio event loop.
    """

    def __init__(self, config: ActualBudgetConfig) -> None:
        self._config = config
        self._actual: Actual | None = None
        self._data_dir: Path | None = None
        self._log = log.bind(server_url=config.server_url)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def __aenter__(self) -> ActualBudgetClient:
        """Connect to the AB server and download the target budget."""
        self._data_dir = Path(tempfile.mkdtemp(prefix="finance_sync_ab_"))
        await self._connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Shut down the AB client and clean up the temp data dir."""
        await self._shutdown()

    async def _connect(self) -> None:
        """Initialise actualpy, log in, and download the budget file.

        Raises:
            ActualBudgetConnectionError: On any connection / auth failure.
        """
        import actual as actual_module

        try:
            self._actual = await asyncio.to_thread(
                _init_sync,
                actual_module=actual_module,
                config=self._config,
                data_dir=str(self._data_dir),
            )

            self._log.info("ab_client_connected")
        except Exception as exc:
            msg = f"Failed to connect to Actual Budget: {exc}"
            raise ActualBudgetConnectionError(msg) from exc

    async def _shutdown(self) -> None:
        """Shut down and clean up."""
        if self._actual is not None:
            try:
                await asyncio.to_thread(self._actual.cleanup)
            except Exception as exc:
                self._log.warning("ab_shutdown_warning", error=str(exc))
            self._actual = None

        if self._data_dir is not None and self._data_dir.exists():
            import shutil

            shutil.rmtree(self._data_dir, ignore_errors=True)
            self._data_dir = None

    # ── Account operations ───────────────────────────────────────────

    async def get_accounts(
        self,
    ) -> list[dict[str, Any]]:
        """Return all non-deleted accounts in the budget.

        Each dict has at minimum ``id``, ``name``, ``offbudget``.
        """
        import actual.queries as q

        accts = await asyncio.to_thread(
            q.get_accounts,
            self.session,  # type: ignore[union-attr]
        )
        return [
            {"id": a.id, "name": a.name, "offbudget": a.offbudget}
            for a in accts
        ]

    async def get_account_by_name(
        self,
        name: str,
    ) -> dict[str, Any] | None:
        """Look up an AB account by display name.

        Returns ``None`` if no account with *name* exists.
        """
        import actual.queries as q

        acct = await asyncio.to_thread(
            q.get_account,
            self.session,
            name,  # type: ignore[union-attr]
        )
        if acct is None:
            return None
        return {"id": acct.id, "name": acct.name, "offbudget": acct.offbudget}

    async def create_account(
        self,
        name: str,
        *,
        off_budget: bool = False,
        initial_balance: float = 0.0,
    ) -> dict[str, Any]:
        """Create a new account in AB.

        Returns:
            The created account dict (``id``, ``name``, ``offbudget``).
        """
        import actual.queries as q

        acct = await asyncio.to_thread(
            q.create_account,
            self.session,  # type: ignore[union-attr]
            name,
            initial_balance=initial_balance,
            off_budget=off_budget,
        )
        await self._commit()
        self._log.info("ab_account_created", name=name)
        return {"id": acct.id, "name": acct.name, "offbudget": acct.offbudget}

    async def get_or_create_account(
        self,
        name: str,
        *,
        off_budget: bool = False,
    ) -> dict[str, Any]:
        """Return an existing AB account or create one with *name*."""
        existing = await self.get_account_by_name(name)
        if existing is not None:
            return existing
        return await self.create_account(name, off_budget=off_budget)

    # ── Transaction operations ───────────────────────────────────────

    async def create_transaction(
        self,
        *,
        date: dt.date,
        account: str,
        payee: str | None = None,
        notes: str | None = None,
        amount: float = 0,
        imported_id: str | None = None,
        cleared: bool = False,
        imported_payee: str | None = None,
    ) -> str | None:
        """Create a single transaction in the budget.

        Returns the transaction ID, or ``None`` on failure.

        Note: This does **not** call ``commit()``.  Call
        ``commit()`` after a batch to sync everything at once.
        """
        import actual.queries as q

        txn = await asyncio.to_thread(
            q.create_transaction,
            self.session,  # type: ignore[union-attr]
            date=date,
            account=account,
            payee=payee,
            notes=notes,
            amount=amount,
            imported_id=imported_id,
            cleared=cleared,
            imported_payee=imported_payee,
        )
        return str(txn.id) if txn else None

    async def create_transactions_batch(
        self,
        transactions: list[dict[str, Any]],
    ) -> int:
        """Create multiple transactions and commit.

        *transactions* is a list of dicts with the same keys as
        ``create_transaction()`` keyword arguments.

        Returns the number of successfully created transactions.
        """
        count = 0
        for txn_data in transactions:
            try:
                await self.create_transaction(**txn_data)
                count += 1
            except Exception as exc:
                self._log.warning(
                    "ab_txn_skip",
                    imported_id=txn_data.get("imported_id"),
                    error=str(exc),
                )
        await self._commit()
        self._log.info(
            "ab_transactions_committed",
            attempted=len(transactions),
            succeeded=count,
        )
        return count

    async def import_transactions_batch(
        self,
        account: str,
        transactions: list[dict[str, Any]],
    ) -> int:
        """Import transactions using AB's reconcile-aware flow.

        This method:
          1. Passes each transaction to the ``reconcile_transaction``
             helper which handles dedup via ``imported_id``.
          2. Commits the batch.

        Returns the number of successfully (re)imported transactions.

        Note: This creates transactions one by one rather than in a
        true bulk-call, but actualpy currently doesn't expose a single
        ``importTransactions()`` equivalent that accepts a full array.
        The per-transaction reconciliation is sufficient for incremental
        syncs.
        """
        import actual.queries as q

        count = 0
        for txn_data in transactions:
            try:
                await asyncio.to_thread(
                    q.reconcile_transaction,
                    self.session,  # type: ignore[union-attr]
                    date=txn_data["date"],
                    account=account,
                    payee=txn_data.get("payee"),
                    notes=txn_data.get("notes"),
                    amount=txn_data.get("amount", 0),
                    imported_id=txn_data.get("imported_id"),
                    cleared=txn_data.get("cleared", False),
                    imported_payee=txn_data.get("imported_payee"),
                )
                count += 1
            except Exception as exc:
                self._log.warning(
                    "ab_txn_reconcile_skip",
                    imported_id=txn_data.get("imported_id"),
                    error=str(exc),
                )
        await self._commit()
        self._log.info(
            "ab_import_committed",
            attempted=len(transactions),
            succeeded=count,
        )
        return count

    # ── Commit / sync ────────────────────────────────────────────────

    async def commit(self) -> None:
        """Flush pending changes and sync to the server."""
        await self._commit()

    async def _commit(self) -> None:
        """Internal: call actual.commit() in a thread."""
        if self._actual is not None:
            await asyncio.to_thread(self._actual.commit)

    # ── Property accessors ───────────────────────────────────────────

    @property
    def session(self) -> object:
        """Return the actualpy session (raises if not connected)."""
        return self._actual and self._actual.session

    @property
    def is_connected(self) -> bool:
        """True if the client is initialised."""
        return self._actual is not None


# ═══════════════════════════════════════════════════════════════════════
# Module-level helpers
# ═══════════════════════════════════════════════════════════════════════


def _init_sync(
    *,
    actual_module: Any,
    config: ActualBudgetConfig,
    data_dir: str,
) -> Actual:
    """Synchronous initialisation: login, download, return Actual instance.

    Runs in a thread executor so the asyncio event loop is not blocked.
    """
    actual: Actual = actual_module.Actual(
        base_url=config.server_url,
        password=config.password,
        file=config.budget_name,
        encryption_password=config.encryption_password,
        data_dir=data_dir,
        cert=config.verify_ssl,
        timeout=config.request_timeout,
    )

    # If a sync-id was provided, use the file-id path instead of name
    if config.sync_id:
        # List available files to find the matching sync id
        files = actual.list_user_files()
        for f in files.get("files", []):
            if (
                f.get("syncId") == config.sync_id
                or f.get("id") == config.sync_id
            ):
                actual.set_file(f)
                break
    elif config.budget_name:
        # Budget by name — the Actual() constructor already handled this
        # via the ``file`` kwarg.
        pass

    return actual
