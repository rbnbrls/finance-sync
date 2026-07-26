"""SyncOrchestrator — end-to-end sync pipeline with transactional outbox.

Flow
====
For a given connector and tenant::

    1. Create SyncRun (status=running)
    2. connector.authenticate()
    3. connector.fetch_accounts()
       → upsert canonical Account records
       → emit outbox messages for created/updated accounts
    4. For each account: connector.fetch_transactions(since)
       → upsert canonical Transaction records
       → emit outbox messages for created/updated transactions
    5. Complete SyncRun (status=completed / failed)

Every domain write (steps 3-5) happens inside a **single** ``UnitOfWork``
transaction.  If any step fails, the whole batch rolls back and the
SyncRun is marked ``failed``.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.connectors.exceptions import (
    ConnectorError,
    PermanentError,
    TransientError,
)
from finance_sync.models import Account, Transaction
from finance_sync.models.enums import (
    ReconciliationRunStatus,
    SyncRunStatus,
    TransactionStatus,
    TransactionType,
)
from finance_sync.sync.outbox import (
    outbox_entity_created,
    outbox_entity_updated,
    outbox_reconciliation_completed,
)
from finance_sync.sync.sync_run import complete_sync_run, start_sync_run

if TYPE_CHECKING:
    from datetime import datetime as dt_type

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
    )

    from finance_sync.connectors.base import Connector
    from finance_sync.connectors.models import (
        CanonicalAccountData,
        CanonicalTransactionData,
        ConnectorConfig,
    )
    from finance_sync.connectors.registry import ConnectorRegistry
    from finance_sync.db.uow import UnitOfWork


logger = structlog.get_logger("finance_sync.sync.orchestrator")


class SyncOrchestrator:
    """Orchestrate a full connector sync cycle.

    Usage::

        orchestrator = SyncOrchestrator(
            session_factory=container.session_factory,
            registry=ConnectorRegistry(),
            tenant_id=tenant_id,
        )
        result = await orchestrator.run_sync(
            provider_type="bunq",
            config=connector_config,
            since=datetime(...),
        )
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ConnectorRegistry,
        tenant_id: str,
        *,
        settings: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._tenant_id = tenant_id
        self._settings = settings

    # ── Config helpers ──────────────────────────────────────────

    @property
    def _reconciliation_after_sync_enabled(self) -> bool:
        """Whether auto-reconciliation after sync is enabled.

        Reads from the injected settings object when available; defaults
        to ``True`` for backward compatibility.
        """
        if self._settings is not None:
            return bool(
                getattr(
                    self._settings,
                    "worker_job_reconciliation_after_sync_enabled",
                    True,
                )
            )
        return True

    # ── Public API ───────────────────────────────────────────────────

    async def run_sync(
        self,
        provider_type: str,
        config: ConnectorConfig,
        *,
        since: dt_type | None = None,
    ) -> SyncResult:
        """Execute a full sync for *provider_type*.

        Args:
            provider_type:  Connector name (e.g. ``"bunq"``).
            config:         ``ConnectorConfig`` with credentials + options.
            since:          Only fetch transactions on or after this time.
                            Defaults to 90 days ago.

        Returns:
            A ``SyncResult`` named tuple with status, counts, and error.
        """
        _since = since or _default_since()
        log = logger.bind(
            provider=provider_type,
            tenant_id=self._tenant_id,
            since=_since.isoformat(),
        )
        log.info("sync_starting")

        connector = self._registry.get_connector(config)

        # ── Run the pipeline ──────────────────────────────────────
        async with self._session_factory() as session:
            result = await self._run_pipeline(
                session, connector, provider_type, _since, log
            )

        if result.status == SyncRunStatus.COMPLETED:
            log.info(
                "sync_completed",
                accounts=result.accounts_synced,
                transactions=result.transactions_synced,
                duration_s=result.duration_s,
            )

            # ── Post-sync reconciliation (opt-in) ──────────────────────
            # Only run automatic reconciliation when the config flag is
            # enabled (default: on).  Checking the flag here lets operators
            # suppress auto-reconciliation without changing the piped
            # workflow — they just set the env var to false.
            if self._reconciliation_after_sync_enabled:
                try:
                    rec_summary = await self.run_reconciliation(
                        date_from=_since,
                    )
                    log.info(
                        "auto_reconciliation_completed",
                        run_id=rec_summary.run_id,
                        status=rec_summary.status.value,
                        findings=rec_summary.finding_count,
                    )
                except Exception:
                    log.error(
                        "auto_reconciliation_failed",
                        error=traceback.format_exc()[:500],
                    )
            else:
                log.debug("auto_reconciliation_skipped_by_config")
        else:
            log.error(
                "sync_failed",
                error=result.error_message,
                duration_s=result.duration_s,
            )

        return result

    # ── Post-sync reconciliation ─────────────────────────────────────

    async def run_reconciliation(
        self,
        *,
        account_ids: list[str] | None = None,
        date_from: dt_type | None = None,
        date_to: dt_type | None = None,
    ) -> ReconciliationRunSummary:
        """Run a reconciliation analysis for this orchestrator's tenant.

        Args:
            account_ids:  Optional subset of accounts to analyze.
            date_from:    Earliest transaction date (default 90 days ago).
            date_to:      Latest transaction date (default now).

        Returns:
            A ``ReconciliationRunSummary`` with the run ID, status, and
            finding count.  If the run completed, a ``reconciliation.completed``
            outbox message is emitted.
        """
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models.enums import ReconciliationRunStatus
        from finance_sync.services.reconciliation import ReconciliationService

        log = logger.bind(
            tenant_id=self._tenant_id,
            reconciliation_type=(
                "post_sync" if account_ids is None else "targeted"
            ),
        )
        log.info("reconciliation_starting")

        svc = ReconciliationService(
            session_factory=self._session_factory,
            tenant_id=self._tenant_id,
        )

        run = await svc.reconcile(
            account_ids=account_ids,
            date_from=date_from,
            date_to=date_to,
        )

        # Emit outbox message for completed reconciliation
        if run.status == ReconciliationRunStatus.COMPLETED:
            try:
                async with self._session_factory() as session:
                    async with UnitOfWork(session) as uow:
                        await outbox_reconciliation_completed(
                            uow,
                            run_id=str(run.id),
                            tenant_id=self._tenant_id,
                            finding_count=run.finding_count or 0,
                            summary=run.summary,
                        )
                    await session.commit()
                log.info(
                    "reconciliation_outbox_emitted",
                    run_id=str(run.id),
                    finding_count=run.finding_count or 0,
                )
            except Exception:
                import traceback

                log.error(
                    "reconciliation_outbox_failed",
                    run_id=str(run.id),
                    error=traceback.format_exc()[:500],
                )

        log.info(
            "reconciliation_completed",
            run_id=str(run.id),
            status=run.status.value,
            finding_count=run.finding_count or 0,
        )

        return ReconciliationRunSummary(
            run_id=str(run.id),
            status=run.status,
            finding_count=run.finding_count or 0,
        )

    # ── Internal pipeline ──────────────────────────────────────────

    async def _run_pipeline(
        self,
        session: AsyncSession,
        connector: Connector,
        provider_type: str,
        since: dt_type,
        log: structlog.BoundLogger,
    ) -> SyncResult:
        from datetime import datetime as _dt

        start_ts = _dt.now(UTC)
        from finance_sync.db.uow import UnitOfWork as _UnitOfWork

        uow = _UnitOfWork(session)
        run = None
        accounts_synced = 0
        transactions_synced = 0

        try:
            async with uow:
                # 1. SyncRun record
                run = await start_sync_run(uow, connector=provider_type)
                log = log.bind(sync_run_id=str(run.id))

                # 2. Authenticate
                await connector.authenticate()
                log.debug("authenticated")

                # 3. Fetch + upsert accounts
                raw_accounts = await connector._rate_limited_fetch_accounts()  # noqa: SLF001
                canonical_accounts = connector.transform_accounts(raw_accounts)

                for ca in canonical_accounts:
                    await self._upsert_account(uow, ca)
                accounts_synced = len(canonical_accounts)
                log.debug("accounts_fetched", count=accounts_synced)

                # 4. Fetch + upsert transactions per account
                for ca in canonical_accounts:
                    raw_txns = await connector._rate_limited_fetch_transactions(  # noqa: SLF001
                        since, account_id=ca.external_account_id
                    )
                    canonical_txns = connector.transform_transactions(raw_txns)

                    # Resolve the canonical account ID for FK
                    acct = await uow.accounts.get_by_external_id(
                        self._tenant_id,
                        provider_type,
                        ca.external_account_id,
                    )
                    if acct is None:
                        log.warning(
                            "account_not_found_for_transactions",
                            external_account_id=ca.external_account_id,
                        )
                        continue

                    for ct in canonical_txns:
                        await self._upsert_transaction(uow, ct, acct.id)
                    transactions_synced += len(canonical_txns)

                log.debug("transactions_fetched", count=transactions_synced)

                # 5. Complete the run
                await complete_sync_run(
                    uow,
                    run,
                    status=SyncRunStatus.COMPLETED,
                    items_processed=accounts_synced + transactions_synced,
                )

            # If we get here, the UoW committed successfully
            end_ts = _dt.now(UTC)
            return SyncResult(
                status=SyncRunStatus.COMPLETED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                error_message=None,
                duration_s=(end_ts - start_ts).total_seconds(),
            )

        except PermanentError as exc:
            end_ts = _dt.now(UTC)
            await self._mark_run_failed(session, run, str(exc), log)
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                error_message=str(exc),
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        except (TransientError, ConnectorError) as exc:
            end_ts = _dt.now(UTC)
            await self._mark_run_failed(session, run, str(exc), log)
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                error_message=str(exc),
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        except Exception:
            end_ts = _dt.now(UTC)
            tb = traceback.format_exc()
            await self._mark_run_failed(session, run, tb, log)
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                error_message=tb,
                duration_s=(end_ts - start_ts).total_seconds(),
            )

    # ── Entity upsert helpers ──────────────────────────────────────

    async def _upsert_account(
        self,
        uow: UnitOfWork,
        ca: CanonicalAccountData,
    ) -> Account:
        """Create or update a canonical Account from connector data."""
        existing = await uow.accounts.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=ca.provider_key,
            external_account_id=ca.external_account_id,
        )

        if existing is not None:
            # Update mutable fields
            changed: dict[str, Any] = {}
            for field in (
                "name",
                "account_type",
                "account_subtype",
                "currency_code",
                "current_balance",
                "available_balance",
                "iso_currency_code",
                "provider_metadata",
                "is_active",
            ):
                new_val = getattr(ca, field, None)
                old_val = getattr(existing, field, None)
                if new_val is not None and new_val != old_val:
                    setattr(existing, field, new_val)
                    changed[field] = new_val

            if changed:
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    entity_type="account",
                    entity_id=str(existing.id),
                    changed_fields=changed,
                    provider_key=ca.provider_key,
                )
            return existing

        # Create new account
        from uuid import uuid4

        account = Account(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=ca.provider_key,
            external_account_id=ca.external_account_id,
            name=ca.name,
            account_type=ca.account_type,
            account_subtype=ca.account_subtype,
            currency_code=ca.currency_code,
            current_balance=ca.current_balance,
            available_balance=ca.available_balance,
            iso_currency_code=ca.iso_currency_code,
            provider_metadata=ca.provider_metadata,
            is_active=ca.is_active,
        )
        uow.session.add(account)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            entity_type="account",
            entity_id=str(account.id),
            entity_data={
                "provider_key": ca.provider_key,
                "external_account_id": ca.external_account_id,
                "name": ca.name,
            },
            provider_key=ca.provider_key,
        )
        return account

    async def _upsert_transaction(
        self,
        uow: UnitOfWork,
        ct: CanonicalTransactionData,
        account_id: str,
    ) -> Transaction:
        """Create or update a canonical Transaction from connector data."""
        existing = await uow.transactions.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=ct.provider_key,
            external_transaction_id=ct.external_transaction_id,
        )

        if existing is not None:
            # Update — only if fields actually changed
            changed: dict[str, Any] = {}
            for field in (
                "amount",
                "currency_code",
                "occurred_at",
                "booked_at",
                "transaction_type",
                "description",
                "quantity",
                "status",
            ):
                new_val = getattr(ct, field, None)
                old_val = getattr(existing, field, None)
                if new_val is not None and str(new_val) != str(old_val):
                    setattr(existing, field, new_val)
                    changed[field] = new_val

            if changed:
                existing.revision = (existing.revision or 0) + 1
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    entity_type="transaction",
                    entity_id=str(existing.id),
                    changed_fields=changed,
                    provider_key=ct.provider_key,
                )
            return existing

        # Create new transaction
        from uuid import uuid4

        txn_type = (
            TransactionType(ct.transaction_type)
            if ct.transaction_type in TransactionType.__members__.values()
            else TransactionType.OTHER
        )
        txn_status = (
            TransactionStatus(ct.status)
            if ct.status in TransactionStatus.__members__.values()
            else TransactionStatus.PENDING
        )

        transaction = Transaction(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=ct.provider_key,
            external_transaction_id=ct.external_transaction_id,
            account_id=account_id,
            amount=Decimal(str(ct.amount)),
            currency_code=ct.currency_code,
            occurred_at=ct.occurred_at,
            booked_at=ct.booked_at,
            transaction_type=txn_type,
            description=ct.description,
            quantity=ct.quantity,
            status=txn_status,
            revision=1,
        )
        uow.session.add(transaction)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            entity_type="transaction",
            entity_id=str(transaction.id),
            entity_data={
                "provider_key": ct.provider_key,
                "external_transaction_id": ct.external_transaction_id,
                "amount": str(ct.amount),
                "currency_code": ct.currency_code,
            },
            provider_key=ct.provider_key,
        )
        return transaction

    # ── Failure handling ───────────────────────────────────────────

    async def _mark_run_failed(
        self,
        session: AsyncSession,
        run: object | None,
        error_message: str,
        log: structlog.BoundLogger,
    ) -> None:
        """Persist a failed SyncRun outside the main UoW (which rolled back)."""
        if run is None:
            log.error("sync_failed_before_run_created", error=error_message)
            return

        # Use a separate transaction to record the failure
        from finance_sync.db.uow import UnitOfWork as _UnitOfWork

        try:
            async with _UnitOfWork(session) as uow:
                # Reload the run in this session if needed
                reloaded = await uow.sync_runs.get(run.id)  # type: ignore[union-attr]
                if reloaded is not None:
                    await complete_sync_run(
                        uow,
                        reloaded,
                        status=SyncRunStatus.FAILED,
                        error_message=error_message[:2048],
                    )
        except Exception as exc:
            log.error(
                "failed_to_persist_failed_sync_run",
                error=str(exc),
            )


# ── Result type ────────────────────────────────────────────────────────


class SyncResult:
    """Outcome of a single sync run."""

    __slots__ = (
        "accounts_synced",
        "duration_s",
        "error_message",
        "status",
        "transactions_synced",
    )

    def __init__(
        self,
        *,
        status: SyncRunStatus,
        accounts_synced: int,
        transactions_synced: int,
        error_message: str | None,
        duration_s: float,
    ) -> None:
        self.status = status
        self.accounts_synced = accounts_synced
        self.transactions_synced = transactions_synced
        self.error_message = error_message
        self.duration_s = duration_s

    def __repr__(self) -> str:
        return (
            f"<SyncResult status={self.status!r} "
            f"accts={self.accounts_synced} txns={self.transactions_synced} "
            f"err={self.error_message!r} dur={self.duration_s:.2f}s>"
        )


class ReconciliationRunSummary:
    """Outcome of a reconciliation analysis run."""

    __slots__ = (
        "finding_count",
        "run_id",
        "status",
    )

    def __init__(
        self,
        *,
        run_id: str,
        status: ReconciliationRunStatus,
        finding_count: int,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.finding_count = finding_count

    def __repr__(self) -> str:
        return (
            f"<ReconciliationRunSummary run_id={self.run_id!r} "
            f"status={self.status!r} findings={self.finding_count}>"
        )


# ── Helpers ────────────────────────────────────────────────────────────


def _default_since() -> dt_type:
    """Return a default ``since`` date of 90 days ago."""
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=90)
