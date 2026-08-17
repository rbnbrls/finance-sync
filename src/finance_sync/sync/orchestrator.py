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
    5. When capability-gated: connector.fetch_holdings()
       → resolve securities and upsert time-versioned Holding records
    6. Complete SyncRun (status=completed / failed)

Every domain write (steps 3-5) happens inside a **single** ``UnitOfWork``
transaction.  If any step fails, the whole batch rolls back and the
SyncRun is marked ``failed``.
"""

from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import structlog
from sqlalchemy import select

from finance_sync.connectors.exceptions import (
    ConnectorError,
    PermanentError,
    TransientError,
)
from finance_sync.models import (
    Account,
    CardTransaction,
    ConnectorState,
    Holding,
    ScheduledPayment,
    Security,
    Transaction,
    UnresolvedSecurity,
)
from finance_sync.models.enums import (
    CardAuthorizationType,
    HoldingSource,
    ReconciliationRunStatus,
    ScheduleFrequency,
    ScheduleStatus,
    SecurityType,
    SyncRunStatus,
    TransactionStatus,
    TransactionType,
)
from finance_sync.observability.metrics import (
    holdings_ingested_total,
    sync_run_duration_seconds,
    sync_runs_total,
    transactions_ingested_total,
    unresolved_securities_total,
)
from finance_sync.sync.outbox import (
    outbox_entity_created,
    outbox_entity_updated,
    outbox_reconciliation_completed,
    outbox_sync_completed,
)
from finance_sync.sync.sync_cursor import (
    RESOURCE_CARD_TRANSACTIONS,
    get_connector_cursors,
    get_cursor,
    upsert_sync_cursor,
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
        CanonicalHoldingData,
        CanonicalScheduledPaymentData,
        CanonicalTransactionData,
        ConnectorConfig,
        RawCardTransaction,
        RawScheduledPayment,
        SecurityReference,
    )
    from finance_sync.connectors.registry import ConnectorRegistry
    from finance_sync.db.uow import UnitOfWork


class _CardsConnector(Protocol):
    """Connector capability for scheduled-payments and card-transactions sync.

    Only the bunq connector implements these methods today; the protocol
    lets the orchestrator call them without widening the base ``Connector``
    interface.
    """

    async def fetch_scheduled_payments(
        self,
    ) -> list[RawScheduledPayment]: ...

    async def fetch_card_transactions(
        self,
        since: dt_type,
        *,
        limit: int | None = None,
    ) -> list[RawCardTransaction]: ...


@runtime_checkable
class _StatefulConnector(Protocol):
    """Connector that can persist opaque runtime state between runs.

    ``bunq`` implements this to keep its installation material (client RSA
    keypair + installation token) stable across sync ticks so a fresh device
    is not registered on every run.  The orchestrator injects the stored
    state before a run and persists the connector's state after it.
    """

    def set_state(self, state: dict[str, Any]) -> None: ...

    def get_state(self) -> dict[str, Any]: ...


logger = structlog.get_logger("finance_sync.sync.orchestrator")


def _values_differ(new_val: Any, old_val: Any) -> bool:
    """Compare a connector value against the stored value for change detection.

    Scale-insensitive for Decimals: a ``Numeric(24,8)`` column reads back as
    e.g. ``Decimal('-13.80000000')``, which must compare equal to the raw
    ``Decimal('-13.80')``.  Comparing via ``str()`` instead makes every
    re-sync look "changed", re-emitting ``{entity}.updated`` with the same
    deterministic outbox idempotency key until the unique constraint aborts
    the whole sync run.
    """
    if isinstance(new_val, Decimal) or isinstance(old_val, Decimal):
        try:
            return Decimal(str(new_val)) != Decimal(str(old_val))
        except (InvalidOperation, TypeError, ValueError):
            pass
    return str(new_val) != str(old_val)


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
        holdings_ingested_total.labels(provider=provider).inc(
            getattr(result, "holdings_synced", 0) or 0
        )
        unresolved_securities_total.labels(provider=provider).inc(
            getattr(result, "unresolved_securities", 0) or 0
        )

    async def run_sync(
        self,
        provider_type: str,
        config: ConnectorConfig,
        *,
        since: dt_type | None = None,
        connection_id: str | None = None,
        selected_accounts: list[str] | None = None,
    ) -> SyncResult:
        """Execute a full sync for *provider_type*.

        Args:
            provider_type:  Connector name (e.g. ``"bunq"``).
            config:         ``ConnectorConfig`` with credentials + options.
            since:          Only fetch transactions on or after this time.
                            Defaults to each account's stored sync
                            cursor (resume), or 90 days ago for accounts
                            on their first sync.
            connection_id:  Optional stable connection (credential) id.
                            When provided, accounts, transactions, sync
                            runs and cursors are scoped to that
                            connection, so identical external ids from
                            another connection never collide.  ``None``
                            keeps the legacy single-connection
                            behaviour.
            selected_accounts:
                            Optional list of external account ids to
                            sync.  Accounts outside the list are skipped
                            (no accounts/transactions stored for them).

        Returns:
            A ``SyncResult`` named tuple with status, counts, and error.
        """
        _since = since or _default_since()
        log = logger.bind(
            provider=provider_type,
            tenant_id=self._tenant_id,
            connection_id=connection_id,
            since=_since.isoformat(),
        )
        log.info("sync_starting")

        connector = self._registry.get_connector(config)

        # Inject persisted connector state (e.g. bunq installation material)
        # before the run so stateful connectors reuse their device identity.
        if isinstance(connector, _StatefulConnector):
            stored = await self._load_connector_state(
                provider_type, connection_id=connection_id
            )
            if stored:
                connector.set_state(stored)
                log.debug("connector_state_injected", provider=provider_type)

        # ── Run the pipeline ──────────────────────────────────────
        async with self._session_factory() as session:
            result = await self._run_pipeline(
                session,
                connector,
                provider_type,
                _since,
                log,
                # An explicit `since` is an operator backfill: it wins
                # over stored cursors.  The default (None) resumes each
                # account from its stored cursor.
                resume=since is None,
                connection_id=connection_id,
                selected_accounts=selected_accounts,
            )

        # Persist new connector state (a freshly created bunq installation)
        # regardless of run outcome — authenticate() runs first, so any
        # returned result means the installation exists server-side.
        if isinstance(connector, _StatefulConnector):
            await self._persist_connector_state(
                provider_type, connector, connection_id=connection_id
            )

        self._record_sync_metrics(provider_type, result)

        if result.status == SyncRunStatus.COMPLETED:
            log.info(
                "sync_completed",
                accounts=result.accounts_synced,
                transactions=result.transactions_synced,
                holdings=result.holdings_synced,
                unresolved_securities=result.unresolved_securities,
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

    # ── Connector state persistence ────────────────────────────────────

    async def _load_connector_state(
        self,
        provider_key: str,
        *,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the stored state blob for ``(tenant, provider)``.

        Used to inject persistent connector state (e.g. the bunq
        installation material) into stateful connectors before a run.

        When *connection_id* is provided the state is scoped to that
        connection so each credential keeps its own installation;
        legacy calls (``None``) load the tenant-wide blob.
        """
        stmt = select(ConnectorState).where(
            ConnectorState.tenant_id == self._tenant_id,
            ConnectorState.provider_key == provider_key,
        )
        if connection_id is not None:
            stmt = stmt.where(  # type: ignore[attr-defined]
                ConnectorState.connection_id == connection_id  # type: ignore[attr-defined]
            )
        async with self._session_factory() as session:
            row = await session.scalar(stmt)
        state = getattr(row, "state", None) if row is not None else None
        if not isinstance(state, dict) or not state:
            return {}
        return dict(cast("dict[str, Any]", state))

    async def _persist_connector_state(
        self,
        provider_key: str,
        connector: Connector,
        *,
        connection_id: str | None = None,
    ) -> None:
        """Persist connector state (e.g. a fresh bunq installation).

        Only stateful connectors expose state; non-empty dicts are upserted
        into ``connector_state`` so the next run reuses the installation.

        When *connection_id* is provided the state is stored per
        connection; legacy calls (``None``) keep the tenant-wide row.
        """
        getter = getattr(connector, "get_state", None)
        state = getter() if callable(getter) else None
        if not isinstance(state, dict) or not state:
            return
        stmt = select(ConnectorState).where(
            ConnectorState.tenant_id == self._tenant_id,
            ConnectorState.provider_key == provider_key,
        )
        if connection_id is not None:
            stmt = stmt.where(  # type: ignore[attr-defined]
                ConnectorState.connection_id == connection_id  # type: ignore[attr-defined]
            )
        async with self._session_factory() as session:
            row = await session.scalar(stmt)
            if row is None:
                session.add(
                    ConnectorState(
                        tenant_id=self._tenant_id,
                        provider_key=provider_key,
                        connection_id=connection_id,
                        state=state,
                    )
                )
            else:
                row.state = state
            await session.commit()

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
                    Defaults to the stored cards cursor, or 90 days ago
                    for the first sync.  Scheduled payments are always
                    fetched in full (they are templates, not an
                    append-only stream).

        Returns:
            A ``BunqCardsSyncResult`` with status, counts, and error.
        """
        # Resume from the stored cards cursor unless an explicit window
        # was given (explicit backfills always win).
        cursor = None
        if since is None:
            async with self._session_factory() as session:
                cursor = await get_cursor(
                    session,
                    tenant_id=self._tenant_id,
                    connector="bunq_cards",
                    resource=RESOURCE_CARD_TRANSACTIONS,
                )
        _since = since or cursor or _default_since()
        log = logger.bind(
            provider="bunq",
            tenant_id=self._tenant_id,
            since=_since.isoformat(),
        )
        log.info("bunq_cards_sync_starting")

        connector = self._registry.get_connector(config)

        # Reuse the persisted bunq installation across cards syncs too.
        if isinstance(connector, _StatefulConnector):
            stored = await self._load_connector_state("bunq")
            if stored:
                connector.set_state(stored)

        async with self._session_factory() as session:
            result = await self._run_cards_pipeline(
                session, connector, _since, log
            )

        if isinstance(connector, _StatefulConnector):
            await self._persist_connector_state("bunq", connector)

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
        *,
        resume: bool = True,
        connection_id: str | None = None,
        selected_accounts: list[str] | None = None,
    ) -> SyncResult:
        from datetime import datetime as _dt

        start_ts = _dt.now(UTC)
        from finance_sync.db.uow import UnitOfWork as _UnitOfWork

        uow = _UnitOfWork(session)
        run = None
        accounts_synced = 0
        transactions_synced = 0
        holdings_synced = 0
        unresolved_keys: set[str] = set()

        # Account selection: when the operator (or the connection's
        # configuration) pinned a subset of accounts, filter the fetched
        # set down before any upsert or cursor work.
        selected_set = set(selected_accounts) if selected_accounts else None

        try:
            async with uow:
                # 1. SyncRun record
                run = await start_sync_run(
                    uow, connector=provider_type, connection_id=connection_id
                )
                log = log.bind(sync_run_id=str(run.id))

                # 2. Authenticate
                await connector.authenticate()
                log.debug("authenticated")

                # 3. Fetch + upsert accounts
                raw_accounts = await connector._rate_limited_fetch_accounts()  # type: ignore[attr-defined]
                canonical_accounts = connector.transform_accounts(raw_accounts)
                if selected_set is not None:
                    canonical_accounts = [
                        ca
                        for ca in canonical_accounts
                        if ca.external_account_id in selected_set
                    ]
                resources = cast(
                    "frozenset[str]",
                    getattr(
                        type(connector),
                        "supported_resources",
                        frozenset[str](),
                    ),
                )
                supports_holdings = "holdings" in resources

                for ca in canonical_accounts:
                    await self._upsert_account(
                        uow, ca, connection_id=connection_id
                    )
                accounts_synced = len(canonical_accounts)
                log.debug("accounts_fetched", count=accounts_synced)

                # 4. Fetch + upsert transactions per account.  Each
                #    account resumes from its own stored cursor when one
                #    exists; accounts without a cursor (first sync, or a
                #    newly added account) fall back to the run-level
                #    ``since`` (explicit backfill or the 90-day default).
                #    An explicit ``since`` (``resume=False``) disables
                #    cursor lookups so backfills cover every account.
                cursors: dict[str, dt_type] = {}
                if resume:
                    cursors = await get_connector_cursors(
                        uow.session,
                        tenant_id=self._tenant_id,
                        connector=provider_type,
                        connection_id=connection_id,
                    )
                if cursors:
                    log.debug(
                        "sync_cursors_loaded",
                        resources=sorted(cursors),
                    )
                for ca in canonical_accounts:
                    acct_since = cursors.get(ca.external_account_id, since)
                    raw_txns = await connector._rate_limited_fetch_transactions(  # type: ignore[attr-defined]
                        acct_since, account_id=ca.external_account_id
                    )
                    canonical_txns = connector.transform_transactions(raw_txns)

                    # Resolve the canonical account ID for FK
                    acct = await uow.accounts.get_by_external_id(
                        self._tenant_id,
                        provider_type,
                        ca.external_account_id,
                        connection_id=connection_id,
                    )
                    if acct is None:
                        log.warning(
                            "account_not_found_for_transactions",
                            external_account_id=ca.external_account_id,
                        )
                        continue

                    for ct in canonical_txns:
                        security_id = None
                        if ct.security_reference is not None:
                            (
                                security,
                                unresolved_key,
                            ) = await self._resolve_security_reference(
                                uow,
                                provider_type,
                                ct.security_reference,
                            )
                            security_id = security.id if security else None
                            if unresolved_key:
                                unresolved_keys.add(unresolved_key)
                        await self._upsert_transaction(
                            uow,
                            ct,
                            acct.id,
                            security_id=security_id,
                            connection_id=connection_id,
                        )
                    transactions_synced += len(canonical_txns)

                    if supports_holdings:
                        raw_holdings = (
                            await connector._rate_limited_fetch_holdings(  # type: ignore[attr-defined]
                                account_id=ca.external_account_id
                            )
                        )
                        canonical_holdings = connector.transform_holdings(
                            raw_holdings
                        )
                        for holding in canonical_holdings:
                            (
                                security,
                                unresolved_key,
                            ) = await self._resolve_security_reference(
                                uow,
                                provider_type,
                                holding.security_reference,
                            )
                            if security is None:
                                if unresolved_key:
                                    unresolved_keys.add(unresolved_key)
                                continue
                            await self._upsert_holding(
                                uow, holding, acct.id, security.id
                            )
                            holdings_synced += 1

                log.debug("transactions_fetched", count=transactions_synced)

                # 5. Complete the run and advance the sync cursors —
                #    both only happen on success, atomically inside the
                #    same UoW transaction (a failure rolls everything
                #    back so the next run retries the same window).
                await complete_sync_run(
                    uow,
                    run,
                    status=SyncRunStatus.COMPLETED,
                    items_processed=(
                        accounts_synced + transactions_synced + holdings_synced
                    ),
                    cursor=start_ts,
                )
                if supports_holdings or unresolved_keys:
                    await outbox_sync_completed(
                        uow,
                        run_id=str(run.id),
                        provider_key=provider_type,
                        accounts=accounts_synced,
                        transactions=transactions_synced,
                        holdings=holdings_synced,
                        unresolved_securities=len(unresolved_keys),
                    )
                for ca in canonical_accounts:
                    await upsert_sync_cursor(
                        uow.session,
                        tenant_id=self._tenant_id,
                        connector=provider_type,
                        resource=ca.external_account_id,
                        cursor=start_ts,
                        connection_id=connection_id,
                    )

            # If we get here, the UoW committed successfully
            end_ts = _dt.now(UTC)
            return SyncResult(
                status=SyncRunStatus.COMPLETED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
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
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
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
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
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
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
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
        cards_connector = cast(_CardsConnector, connector)
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
                raw_schedules = await cards_connector.fetch_scheduled_payments()
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
                raw_card_txns = await cards_connector.fetch_card_transactions(
                    since
                )
                canonical_card_txns = connector.transform_card_transactions(
                    raw_card_txns
                )
                for cct in canonical_card_txns:
                    await self._upsert_card_transaction(uow, cct)
                card_txns_synced = len(canonical_card_txns)
                log.debug("card_transactions_fetched", count=card_txns_synced)

                # 6. Complete the run and advance the cards cursor on
                #    success (scheduled payments are templates and are
                #    always fetched in full — no cursor for them).
                await complete_sync_run(
                    uow,
                    run,
                    status=SyncRunStatus.COMPLETED,
                    items_processed=schedules_synced + card_txns_synced,
                    cursor=start_ts,
                )
                await upsert_sync_cursor(
                    uow.session,
                    tenant_id=self._tenant_id,
                    connector="bunq_cards",
                    resource=RESOURCE_CARD_TRANSACTIONS,
                    cursor=start_ts,
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
        *,
        connection_id: str | None = None,
    ) -> Account:
        """Create or update a canonical Account from connector data.

        When *connection_id* is provided the lookup is scoped to that
        connection and new rows carry it, so equal external ids from
        different connections resolve to distinct accounts.
        """
        existing = await uow.accounts.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=ca.provider_key,
            external_account_id=ca.external_account_id,
            connection_id=connection_id,
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
            connection_id=connection_id,
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
        *,
        security_id: str | None = None,
        connection_id: str | None = None,
    ) -> Transaction:
        """Create or update a canonical Transaction from connector data.

        When *connection_id* is provided the lookup is scoped to that
        connection and new rows carry it, so equal external ids from
        different connections resolve to distinct transactions.
        """
        existing = await uow.transactions.get_by_external_id(
            tenant_id=self._tenant_id,
            provider_key=ct.provider_key,
            external_transaction_id=ct.external_transaction_id,
            connection_id=connection_id,
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
                "unit_price",
                "fee_amount",
                "fee_currency_code",
                "status",
                "amount_in_base",
                "base_currency_code",
                "fx_rate",
                "provider_fingerprint",
            ):
                new_val = getattr(ct, field, None)
                old_val = getattr(existing, field, None)
                if new_val is not None and _values_differ(new_val, old_val):
                    setattr(existing, field, new_val)
                    changed[field] = new_val

            if security_id is not None and security_id != existing.security_id:
                existing.security_id = security_id
                changed["security_id"] = security_id

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
            connection_id=connection_id,
            external_transaction_id=ct.external_transaction_id,
            account_id=account_id,
            security_id=security_id,
            amount=Decimal(str(ct.amount)),
            currency_code=ct.currency_code,
            amount_in_base=(
                Decimal(str(ct.amount_in_base))
                if ct.amount_in_base is not None
                else None
            ),
            base_currency_code=ct.base_currency_code,
            fx_rate=(
                Decimal(str(ct.fx_rate)) if ct.fx_rate is not None else None
            ),
            occurred_at=ct.occurred_at,
            booked_at=ct.booked_at,
            transaction_type=txn_type,
            description=ct.description,
            quantity=ct.quantity,
            unit_price=ct.unit_price,
            fee_amount=ct.fee_amount,
            fee_currency_code=ct.fee_currency_code,
            status=txn_status,
            provider_fingerprint=ct.provider_fingerprint,
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

    async def _upsert_holding(
        self,
        uow: UnitOfWork,
        holding: CanonicalHoldingData,
        account_id: str,
        security_id: str,
    ) -> Holding:
        """Idempotently store one time-versioned holding snapshot."""
        try:
            source = HoldingSource(holding.source)
        except ValueError:
            source = HoldingSource.PROVIDER_SYNC
        existing = await uow.holdings.get_by_snapshot(
            self._tenant_id,
            account_id,
            security_id,
            holding.observed_at,
            source.value,
        )
        values = {
            "quantity": Decimal(str(holding.quantity)),
            "cost_basis": (
                Decimal(str(holding.cost_basis))
                if holding.cost_basis is not None
                else None
            ),
            "cost_basis_currency": holding.cost_basis_currency,
            "market_value": (
                Decimal(str(holding.market_value))
                if holding.market_value is not None
                else None
            ),
            "currency_code": holding.currency_code,
            "price": (
                Decimal(str(holding.price))
                if holding.price is not None
                else None
            ),
            "price_currency": holding.price_currency,
        }
        if existing is not None:
            changed: dict[str, Any] = {}
            for field, new_value in values.items():
                if _values_differ(new_value, getattr(existing, field)):
                    setattr(existing, field, new_value)
                    changed[field] = new_value
            if changed:
                await uow.session.flush()
                await outbox_entity_updated(
                    uow,
                    entity_type="holding",
                    entity_id=str(existing.id),
                    changed_fields={"snapshot_updated": True},
                    provider_key=holding.provider_key,
                )
            return existing

        from uuid import uuid4

        entity = Holding(
            id=uuid4(),
            tenant_id=self._tenant_id,
            account_id=account_id,
            security_id=security_id,
            observed_at=holding.observed_at,
            source=source,
            **values,
        )
        uow.session.add(entity)
        await uow.session.flush()
        await outbox_entity_created(
            uow,
            entity_type="holding",
            entity_id=str(entity.id),
            entity_data={"observed_at": holding.observed_at.isoformat()},
            provider_key=holding.provider_key,
        )
        return entity

    async def _resolve_security_reference(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
    ) -> tuple[Security | None, str | None]:
        """Resolve ISIN-first, avoiding ambiguous ticker matches.

        Unknown but sufficiently identified provider instruments become new
        canonical securities. Ambiguous or incomplete references enter the
        existing manual-resolution queue. A previously manual-resolved queue
        item is honoured before automatic matching.
        """
        external_id = reference.provider_identifier()
        if external_id:
            queued = await uow.unresolved_securities.list(
                UnresolvedSecurity.provider_key == provider_key,
                UnresolvedSecurity.external_security_id == external_id,
                limit=1,
            )
            if queued and queued[0].resolved_security_id:
                resolved = await uow.securities.get(
                    queued[0].resolved_security_id
                )
                if resolved is not None:
                    return resolved, None

        candidates: list[Security] = []
        if reference.isin:
            candidates = await uow.securities.list(
                Security.isin == reference.isin.upper()
            )
        if not candidates and reference.figi:
            candidates = await uow.securities.list(
                Security.figi == reference.figi.upper()
            )
        if (
            not candidates
            and reference.ticker
            and reference.external_id is None
        ):
            candidates = await uow.securities.list(
                Security.ticker == reference.ticker.upper()
            )
            if reference.currency_code:
                currency_matches = [
                    item
                    for item in candidates
                    if item.currency_code == reference.currency_code.upper()
                ]
                candidates = currency_matches

        if len(candidates) == 1:
            if reference.external_id:
                await self._queue_unresolved_security(
                    uow,
                    provider_key,
                    reference,
                    resolved_security_id=str(candidates[0].id),
                    resolution_method=(
                        "auto_isin"
                        if reference.isin
                        else "auto_figi"
                        if reference.figi
                        else "auto_ticker"
                    ),
                )
            return candidates[0], None
        if len(candidates) > 1:
            return None, await self._queue_unresolved_security(
                uow, provider_key, reference
            )

        can_create = bool(
            reference.isin
            or reference.figi
            or (
                reference.external_id
                and reference.ticker
                and reference.name
                and reference.currency_code
            )
        )
        if can_create:
            try:
                security_type = SecurityType(
                    reference.security_type or SecurityType.OTHER.value
                )
            except ValueError:
                security_type = SecurityType.OTHER
            from uuid import uuid4

            security = Security(
                id=uuid4(),
                isin=reference.isin.upper() if reference.isin else None,
                figi=(
                    reference.figi.upper()
                    if reference.figi and len(reference.figi) <= 12
                    else None
                ),
                ticker=(reference.ticker.upper() if reference.ticker else None),
                name=reference.name
                or reference.ticker
                or reference.isin
                or reference.figi
                or "Unknown security",
                security_type=security_type,
                currency_code=(reference.currency_code or "EUR").upper(),
            )
            uow.session.add(security)
            await uow.session.flush()
            if reference.external_id:
                await self._queue_unresolved_security(
                    uow,
                    provider_key,
                    reference,
                    resolved_security_id=str(security.id),
                    resolution_method=(
                        "auto_isin"
                        if reference.isin
                        else "auto_figi"
                        if reference.figi
                        else "provider_instrument"
                    ),
                )
            return security, None

        return None, await self._queue_unresolved_security(
            uow, provider_key, reference
        )

    async def _queue_unresolved_security(
        self,
        uow: UnitOfWork,
        provider_key: str,
        reference: SecurityReference,
        *,
        resolved_security_id: str | None = None,
        resolution_method: str | None = None,
    ) -> str | None:
        """Create or refresh a provider identity mapping/queue item."""
        external_id = reference.provider_identifier()
        if not external_id:
            # No stable key means silently storing the row would itself create
            # an unresolvable duplicate stream. It is still counted by type.
            return "missing-provider-identifier"
        rows = await uow.unresolved_securities.list(
            UnresolvedSecurity.provider_key == provider_key,
            UnresolvedSecurity.external_security_id == external_id,
            limit=1,
        )
        metadata = dict(reference.provider_metadata or {})
        if reference.venue:
            metadata["venue"] = reference.venue
        raw_metadata = (
            json.dumps(metadata, sort_keys=True) if metadata else None
        )
        if rows:
            unresolved = rows[0]
            unresolved.raw_isin = reference.isin
            unresolved.raw_figi = reference.figi
            unresolved.raw_ticker = reference.ticker
            unresolved.raw_name = reference.name
            unresolved.raw_currency_code = reference.currency_code
            unresolved.raw_metadata = raw_metadata
            unresolved.resolved_security_id = resolved_security_id
            unresolved.resolution_method = resolution_method
            await uow.session.flush()
        else:
            from uuid import uuid4

            uow.session.add(
                UnresolvedSecurity(
                    id=uuid4(),
                    provider_key=provider_key,
                    external_security_id=external_id,
                    raw_isin=reference.isin,
                    raw_figi=reference.figi,
                    raw_ticker=reference.ticker,
                    raw_name=reference.name,
                    raw_currency_code=reference.currency_code,
                    raw_metadata=raw_metadata,
                    resolved_security_id=resolved_security_id,
                    resolution_method=resolution_method,
                )
            )
            await uow.session.flush()
        return external_id

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
                if new_val is not None and _values_differ(new_val, old_val):
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
                if new_val is not None and _values_differ(new_val, old_val):
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
                    connection_id = getattr(run, "connection_id", None)
                    uow.session.add(
                        _SyncRun(
                            connector=connector,
                            connection_id=connection_id,
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
        "holdings_synced",
        "status",
        "transactions_synced",
        "unresolved_securities",
    )

    def __init__(
        self,
        *,
        status: SyncRunStatus,
        accounts_synced: int,
        transactions_synced: int,
        error_message: str | None,
        duration_s: float,
        holdings_synced: int = 0,
        unresolved_securities: int = 0,
    ) -> None:
        self.status = status
        self.accounts_synced = accounts_synced
        self.transactions_synced = transactions_synced
        self.holdings_synced = holdings_synced
        self.unresolved_securities = unresolved_securities
        self.error_message = error_message
        self.duration_s = duration_s

    def __repr__(self) -> str:
        return (
            f"<SyncResult status={self.status!r} "
            f"accts={self.accounts_synced} txns={self.transactions_synced} "
            f"holdings={self.holdings_synced} "
            f"unresolved={self.unresolved_securities} "
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
