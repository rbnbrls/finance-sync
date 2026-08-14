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
from finance_sync.models import (
    Account,
    CardTransaction,
    ScheduledPayment,
    Transaction,
)
from finance_sync.models.enums import (
    CardAuthorizationType,
    ReconciliationRunStatus,
    ScheduleFrequency,
    ScheduleStatus,
    SyncRunStatus,
    TransactionStatus,
    TransactionType,
)
from finance_sync.observability.metrics import (
    sync_run_duration_seconds,
    sync_runs_total,
    transactions_ingested_total,
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
        CanonicalCardTransactionData,
        CanonicalScheduledPaymentData,
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

    @staticmethod
    def _record_sync_metrics(
        provider: str,
        result: SyncResult | BunqCardsSyncResult,
    ) -> None:
        """Record Prometheus metrics for a completed sync run.

        Increments ``sync_runs_total`` with the run status and records
        the run duration plus ingested transaction count.  Both
        ``SyncResult`` and ``BunqCardsSyncResult`` expose ``status``,
        ``duration_s`` and ``transactions_synced``/``card_transactions_synced``.
        """
        status = (
            result.status.value
            if hasattr(result.status, "value")
            else str(result.status)
        )
        sync_runs_total.labels(provider=provider, status=status).inc()
        sync_run_duration_seconds.labels(provider=provider).set(
            result.duration_s
        )
        ingested = getattr(
            result,
            "transactions_synced",
            getattr(result, "card_transactions_synced", 0),
        )
        transactions_ingested_total.labels(provider=provider).inc(ingested or 0)

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

        self._record_sync_metrics(provider_type, result)

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

    # ── Bunq cards / scheduled payments sync ──────────────────────────

    async def run_bunq_cards_sync(
        self,
        config: ConnectorConfig,
        *,
        since: dt_type | None = None,
    ) -> BunqCardsSyncResult:
        """Fetch scheduled payments + card transactions and upsert them.

        Runs as an independent sync cycle (connector ``bunq_cards``) so
        the hourly cards/schedules cadence does not depend on the main
        15-minute transaction sync.  Upserts are idempotent: both tables
        carry a ``(tenant_id, provider_key, external_*)`` unique
        constraint, so re-runs update in place instead of duplicating.

        Args:
            config: ``ConnectorConfig`` with credentials + options.
            since:  Only fetch card transactions on or after this time.
                    Defaults to 90 days ago.  Scheduled payments are
                    always fetched in full (they are templates, not an
                    append-only stream).

        Returns:
            A ``BunqCardsSyncResult`` with status, counts, and error.
        """
        _since = since or _default_since()
        log = logger.bind(
            provider="bunq",
            tenant_id=self._tenant_id,
            since=_since.isoformat(),
        )
        log.info("bunq_cards_sync_starting")

        connector = self._registry.get_connector(config)

        async with self._session_factory() as session:
            result = await self._run_cards_pipeline(
                session, connector, _since, log
            )

        self._record_sync_metrics("bunq_cards", result)

        if result.status == SyncRunStatus.COMPLETED:
            log.info(
                "bunq_cards_sync_completed",
                schedules=result.schedules_synced,
                card_transactions=result.card_transactions_synced,
                duration_s=result.duration_s,
            )
        else:
            log.error(
                "bunq_cards_sync_failed",
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
                raw_accounts = await connector._rate_limited_fetch_accounts()  # type: ignore[attr-defined]
                canonical_accounts = connector.transform_accounts(raw_accounts)

                for ca in canonical_accounts:
                    await self._upsert_account(uow, ca)
                accounts_synced = len(canonical_accounts)
                log.debug("accounts_fetched", count=accounts_synced)

                # 4. Fetch + upsert transactions per account
                for ca in canonical_accounts:
                    raw_txns = await connector._rate_limited_fetch_transactions(  # type: ignore[attr-defined]
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

    # ── Cards pipeline ─────────────────────────────────────────────

    async def _run_cards_pipeline(
        self,
        session: AsyncSession,
        connector: Connector,
        since: dt_type,
        log: structlog.BoundLogger,
    ) -> BunqCardsSyncResult:
        """Internal pipeline for bunq cards + scheduled payments.

        Steps (all inside one ``UnitOfWork`` transaction):

            1. SyncRun record (connector ``bunq_cards``)
            2. authenticate()
            3. Upsert accounts (schedules need the FK target)
            4. fetch_scheduled_payments() → upsert ScheduledPayment
            5. fetch_card_transactions(since) → upsert CardTransaction
            6. Complete SyncRun

        Any failure rolls back the whole batch and marks the run failed.
        """
        from datetime import datetime as _dt

        start_ts = _dt.now(UTC)
        from finance_sync.db.uow import UnitOfWork as _UnitOfWork

        uow = _UnitOfWork(session)
        run = None
        schedules_synced = 0
        card_txns_synced = 0

        try:
            async with uow:
                # 1. SyncRun record
                run = await start_sync_run(uow, connector="bunq_cards")
                log = log.bind(sync_run_id=str(run.id))

                # 2. Authenticate
                await connector.authenticate()
                log.debug("authenticated")

                # 3. Fetch + upsert accounts so schedules can resolve
                #    their account FK (mirrors the main pipeline).
                raw_accounts = await connector._rate_limited_fetch_accounts()  # type: ignore[attr-defined]
                canonical_accounts = connector.transform_accounts(raw_accounts)
                for ca in canonical_accounts:
                    await self._upsert_account(uow, ca)
                log.debug("accounts_fetched", count=len(canonical_accounts))

                # 4. Scheduled payments (full fetch — templates, not a
                #    since-filtered stream).
                raw_schedules = await connector.fetch_scheduled_payments()
                canonical_schedules = connector.transform_scheduled_payments(
                    raw_schedules
                )
                for cs in canonical_schedules:
                    acct = await uow.accounts.get_by_external_id(
                        self._tenant_id,
                        cs.provider_key,
                        cs.external_account_id,
                    )
                    if acct is None:
                        log.warning(
                            "account_not_found_for_schedule",
                            external_account_id=cs.external_account_id,
                        )
                        continue
                    await self._upsert_scheduled_payment(uow, cs, acct.id)
                schedules_synced = len(canonical_schedules)
                log.debug("schedules_fetched", count=schedules_synced)

                # 5. Card transactions (since-filtered)
                raw_card_txns = await connector.fetch_card_transactions(since)
                canonical_card_txns = connector.transform_card_transactions(
                    raw_card_txns
                )
                for cct in canonical_card_txns:
                    await self._upsert_card_transaction(uow, cct)
                card_txns_synced = len(canonical_card_txns)
                log.debug("card_transactions_fetched", count=card_txns_synced)

                # 6. Complete the run
                await complete_sync_run(
                    uow,
                    run,
                    status=SyncRunStatus.COMPLETED,
                    items_processed=schedules_synced + card_txns_synced,
                )

            end_ts = _dt.now(UTC)
            return BunqCardsSyncResult(
                status=SyncRunStatus.COMPLETED,
                schedules_synced=schedules_synced,
                card_transactions_synced=card_txns_synced,
                error_message=None,
                duration_s=(end_ts - start_ts).total_seconds(),
            )

        except (PermanentError, TransientError, ConnectorError) as exc:
            end_ts = _dt.now(UTC)
            await self._mark_run_failed(session, run, str(exc), log)
            return BunqCardsSyncResult(
                status=SyncRunStatus.FAILED,
                schedules_synced=schedules_synced,
                card_transactions_synced=card_txns_synced,
                error_message=str(exc),
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        except Exception:
            end_ts = _dt.now(UTC)
            tb = traceback.format_exc()
            await self._mark_run_failed(session, run, tb, log)
            return BunqCardsSyncResult(
                status=SyncRunStatus.FAILED,
                schedules_synced=schedules_synced,
                card_transactions_synced=card_txns_synced,
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

    async def _upsert_scheduled_payment(
        self,
        uow: UnitOfWork,
        csp: CanonicalScheduledPaymentData,
        account_id: str,
    ) -> ScheduledPayment:
        """Create or update a ScheduledPayment from connector data.

        Idempotent: looked up by the ``(tenant, provider, external
        schedule id)`` unique constraint — a re-run updates mutable
        fields instead of inserting a duplicate.
        """
        existing = await uow.scheduled_payments.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=csp.provider_key,
            external_schedule_id=csp.external_schedule_id,
        )

        if existing is not None:
            changed: dict[str, Any] = {}
            for field in (
                "amount",
                "currency_code",
                "frequency",
                "interval",
                "next_execution_date",
                "end_date",
                "max_executions",
                "execution_count",
                "counterparty_name",
                "counterparty_iban",
                "description",
                "status",
            ):
                new_val = getattr(csp, field, None)
                old_val = getattr(existing, field, None)
                if new_val is not None and str(new_val) != str(old_val):
                    setattr(existing, field, new_val)
                    changed[field] = new_val

            if changed:
                await uow.session.flush()
            return existing

        # Create new scheduled payment
        from uuid import uuid4

        frequency = (
            ScheduleFrequency(csp.frequency)
            if csp.frequency in ScheduleFrequency.__members__.values()
            else ScheduleFrequency.CUSTOM
        )
        status = (
            ScheduleStatus(csp.status)
            if csp.status in ScheduleStatus.__members__.values()
            else ScheduleStatus.ACTIVE
        )

        schedule = ScheduledPayment(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=csp.provider_key,
            external_schedule_id=csp.external_schedule_id,
            account_id=account_id,
            amount=Decimal(str(csp.amount)),
            currency_code=csp.currency_code,
            frequency=frequency,
            interval=csp.interval,
            next_execution_date=csp.next_execution_date,
            end_date=csp.end_date,
            max_executions=csp.max_executions,
            execution_count=csp.execution_count or 0,
            counterparty_name=csp.counterparty_name,
            counterparty_iban=csp.counterparty_iban,
            description=csp.description,
            status=status,
        )
        uow.session.add(schedule)
        await uow.session.flush()
        return schedule

    async def _upsert_card_transaction(
        self,
        uow: UnitOfWork,
        cct: CanonicalCardTransactionData,
    ) -> CardTransaction:
        """Create or update a CardTransaction from connector data.

        Idempotent: looked up by the ``(tenant, provider, external card
        transaction id)`` unique constraint.

        The canonical record's ``external_account_id`` is the *card*
        identifier for bunq (card payments are card-scoped, not
        account-scoped), so the account link is best-effort: it is set
        when the id resolves to a known account, otherwise ``None``.
        """
        existing = await uow.card_transactions.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=cct.provider_key,
            external_card_transaction_id=cct.external_card_transaction_id,
        )

        if existing is not None:
            changed: dict[str, Any] = {}
            for field in (
                "amount",
                "currency_code",
                "merchant_name",
                "merchant_city",
                "merchant_country",
                "mcc",
                "card_id",
                "card_type",
                "card_last_four",
                "occurred_at",
                "booked_at",
                "authorization_type",
                "description",
                "status",
            ):
                new_val = getattr(cct, field, None)
                old_val = getattr(existing, field, None)
                if new_val is not None and str(new_val) != str(old_val):
                    setattr(existing, field, new_val)
                    changed[field] = new_val

            if changed:
                await uow.session.flush()
            return existing

        # Best-effort account resolution (card id may not be an account)
        account_id: str | None = None
        if cct.external_account_id:
            acct = await uow.accounts.get_by_external_id(
                tenant_id=self._tenant_id,
                provider_key=cct.provider_key,
                external_account_id=cct.external_account_id,
            )
            if acct is not None:
                account_id = acct.id

        # Create new card transaction
        from uuid import uuid4

        # Canonical card data carries no transaction_type — card payments
        # are always classified as card_payment unless a provider says
        # otherwise.
        raw_txn_type = getattr(cct, "transaction_type", None)
        txn_type = (
            TransactionType(raw_txn_type)
            if raw_txn_type in TransactionType.__members__.values()
            else TransactionType.CARD_PAYMENT
        )
        auth_type = (
            CardAuthorizationType(cct.authorization_type)
            if cct.authorization_type
            in CardAuthorizationType.__members__.values()
            else CardAuthorizationType.OTHER
        )
        txn_status = (
            TransactionStatus(cct.status)
            if cct.status in TransactionStatus.__members__.values()
            else TransactionStatus.PENDING
        )

        card_txn = CardTransaction(
            id=uuid4(),
            tenant_id=self._tenant_id,
            provider_key=cct.provider_key,
            external_card_transaction_id=cct.external_card_transaction_id,
            account_id=account_id,
            amount=Decimal(str(cct.amount)),
            currency_code=cct.currency_code,
            merchant_name=cct.merchant_name,
            merchant_city=cct.merchant_city,
            merchant_country=cct.merchant_country,
            mcc=cct.mcc,
            card_id=cct.card_id,
            card_type=cct.card_type,
            card_last_four=cct.card_last_four,
            occurred_at=cct.occurred_at,
            booked_at=cct.booked_at,
            transaction_type=txn_type,
            authorization_type=auth_type,
            description=cct.description,
            status=txn_status,
        )
        uow.session.add(card_txn)
        await uow.session.flush()
        return card_txn

    # ── Failure handling ───────────────────────────────────────────

    async def _mark_run_failed(
        self,
        session: AsyncSession,
        run: object | None,
        error_message: str,
        log: structlog.BoundLogger,
    ) -> None:
        """Persist a failed SyncRun outside the main UoW (which rolled back).

        The in-flight ``SyncRun`` row was rolled back with the transaction,
        so it cannot be reloaded — instead a fresh ``FAILED`` row is
        inserted so failed runs stay observable (alerting relies on them).
        """
        if run is None:
            log.error("sync_failed_before_run_created", error=error_message)
            return

        # Use a separate transaction to record the failure
        from finance_sync.db.uow import UnitOfWork as _UnitOfWork
        from finance_sync.models import SyncRun as _SyncRun

        try:
            async with _UnitOfWork(session) as uow:
                # Reload the run in this session if it survived the rollback
                run_id = getattr(run, "id", None)
                reloaded = (
                    await uow.sync_runs.get(run_id)
                    if run_id is not None
                    else None
                )
                if reloaded is not None:
                    await complete_sync_run(
                        uow,
                        reloaded,
                        status=SyncRunStatus.FAILED,
                        error_message=error_message[:2048],
                    )
                else:
                    # The original row never made it to the DB — insert a
                    # fresh FAILED record so the failure is observable.
                    connector = getattr(run, "connector", None) or "unknown"
                    uow.session.add(
                        _SyncRun(
                            connector=connector,
                            status=SyncRunStatus.FAILED,
                            completed_at=datetime.now(UTC),
                            error_message=error_message[:2048],
                        )
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


class BunqCardsSyncResult:
    """Outcome of a bunq cards/scheduled-payments sync run."""

    __slots__ = (
        "card_transactions_synced",
        "duration_s",
        "error_message",
        "schedules_synced",
        "status",
    )

    def __init__(
        self,
        *,
        status: SyncRunStatus,
        schedules_synced: int,
        card_transactions_synced: int,
        error_message: str | None,
        duration_s: float,
    ) -> None:
        self.status = status
        self.schedules_synced = schedules_synced
        self.card_transactions_synced = card_transactions_synced
        self.error_message = error_message
        self.duration_s = duration_s

    def __repr__(self) -> str:
        return (
            f"<BunqCardsSyncResult status={self.status!r} "
            f"schedules={self.schedules_synced} "
            f"cards={self.card_transactions_synced} "
            f"err={self.error_message!r} dur={self.duration_s:.2f}s>"
        )


# ── Helpers ────────────────────────────────────────────────────────────


def _default_since() -> dt_type:
    """Return a default ``since`` date of 90 days ago."""
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=90)
