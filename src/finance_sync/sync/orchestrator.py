"""Coordinate connector sync pipelines and transaction lifecycle."""

from __future__ import annotations

import traceback
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import select

from finance_sync.connectors.exceptions import (
    ConnectorError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.models import (
    ConnectorState,
    Credential,
)
from finance_sync.models.enums import (
    ReconciliationRunStatus,
    SyncRunStatus,
)
from finance_sync.observability.connector_metrics import (
    record_connector_operation,
)
from finance_sync.observability.glitchtip import capture_connector_exception
from finance_sync.sync.cards_pipeline import (
    BunqCardsSyncResult,
    CardsSyncMixin,
    StatefulConnector,
)

__all__ = ["BunqCardsSyncResult", "SyncOrchestrator"]
from finance_sync.services.connector_compatibility import (
    default_contract_paths,
    evaluate_connector,
    load_json,
)
from finance_sync.sync.context import SyncContext
from finance_sync.sync.errors import (
    InvalidSinceError,
    SyncErrorKind,
    categorize_sync_error,
    classify_sync_error,
    safe_sync_error_message,
    validate_since,
)
from finance_sync.sync.outbox import (
    outbox_reconciliation_completed,
    outbox_sync_completed,
)
from finance_sync.sync.persistence import (
    PersistenceContext,
    SyncPersistence,
)
from finance_sync.sync.results import ReconciliationRunSummary, SyncResult
from finance_sync.sync.stages.accounts import AccountSyncStage
from finance_sync.sync.stages.holdings import HoldingsSyncStage
from finance_sync.sync.stages.transactions import TransactionSyncStage
from finance_sync.sync.sync_cursor import (
    get_connector_cursors,
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
        ConnectorConfig,
    )
    from finance_sync.connectors.registry import ConnectorRegistry

logger = structlog.get_logger("finance_sync.sync.orchestrator")


class SyncOrchestrator(CardsSyncMixin):
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

    # ── Connection outcome tracking ────────────────────────────────

    async def _mark_connection_attempt(
        self,
        connection_id: str | None,
        log: structlog.BoundLogger,
    ) -> None:
        """Record ``last_attempt_at`` on the connection row.

        Runs before the sync pipeline starts so the control-panel UI can
        show a live attempt timestamp.  Purely informational metadata —
        failures here must never abort the sync itself.
        """
        if not connection_id:
            return
        try:
            async with self._session_factory() as session:
                cred = await session.get(Credential, connection_id)
                if cred is None or str(cred.tenant_id) != self._tenant_id:
                    log.warning(
                        "connection_not_found_for_attempt",
                        connection_id=connection_id,
                    )
                    return
                cred.last_attempt_at = datetime.now(UTC)
                await session.commit()
        except Exception:
            log.warning(
                "connection_attempt_mark_failed",
                connection_id=connection_id,
                error=traceback.format_exc()[:200],
            )

    async def _record_connection_outcome(
        self,
        connection_id: str | None,
        secrets: dict[str, str] | None,
        status: SyncRunStatus,
        error_message: str | None,
        log: structlog.BoundLogger,
        result: SyncResult | None = None,
    ) -> None:
        """Persist ``last_success_at`` / sanitised ``last_error``.

        Called after every run for multi-connection syncs: a completed
        run stamps ``last_success_at`` and clears the previous error; a
        failed run stores a secret-scrubbed, truncated error message so
        the API/UI ``last_error`` field can never leak credentials.
        """
        if not connection_id:
            return
        try:
            async with self._session_factory() as session:
                cred = await session.get(Credential, connection_id)
                if cred is None or str(cred.tenant_id) != self._tenant_id:
                    return
                if status == SyncRunStatus.COMPLETED:
                    cred.last_success_at = datetime.now(UTC)
                    cred.last_error = None
                    cred.last_error_category = None
                    cred.retry_after_at = None
                    cred.rate_limited_at = None
                    cred.rate_limit_attempts = 0
                    cred.rate_limit_scope = None
                    cred.last_http_status = None
                else:
                    from finance_sync.utils.redaction import sanitize_error

                    cred.last_error = sanitize_error(
                        str(error_message or "Unknown error"),
                        tuple((secrets or {}).values()),
                    )
                    from finance_sync.sync.errors import categorize_export_error

                    cred.last_error_category = categorize_export_error(
                        error_message
                    )
                    if (
                        result is not None
                        and result.error_category == "rate_limited"
                    ):
                        cred.rate_limited_at = datetime.now(UTC)
                        cred.retry_after_at = result.retry_after_at
                        cred.rate_limit_attempts = result.rate_limit_attempts
                        cred.rate_limit_scope = result.rate_limit_scope
                        cred.last_http_status = 429
                await session.commit()
        except Exception:
            log.warning(
                "connection_outcome_record_failed",
                connection_id=connection_id,
                error=traceback.format_exc()[:200],
            )

    async def run_sync(
        self,
        provider_type: str,
        config: ConnectorConfig,
        *,
        since: dt_type | str | None = None,
        connection_id: str | None = None,
        selected_accounts: list[str] | None = None,
    ) -> SyncResult:
        """Execute a full sync for *provider_type*.

        Args:
            provider_type:  Connector name (e.g. 'bunq').
            config:         ``ConnectorConfig`` with credentials + options.
            since:          Only fetch transactions on or after this time.
                            Accepts an aware/naive :class:`datetime` or an
                            ISO-8601 string (truncated forms allowed);
                            missing/empty falls back to each account's
                            stored sync cursor (resume), or 90 days ago
                            for accounts on their first sync.  A value
                            that cannot be parsed yields a controlled
                            FAILED result instead of an unhandled error.
            connection_id:  Stable connection (credential) id this sync
                            belongs to.  When provided, the run is scoped
                            to that connection: accounts, transactions,
                            sync runs, cursors and connector state are
                            persisted with the connection id so identical
                            provider ids from two connections never
                            collide, and the connection's
                            ``last_attempt_at`` / ``last_success_at`` /
                            ``last_error`` fields are updated for the UI.
            selected_accounts:
                            Provider account ids to sync for this
                            connection.  When provided, only those
                            accounts (and their transactions/holdings)
                            are fetched and persisted; ``None``/empty
                            means 'sync all accounts the provider offers'.

        Returns:
            A ``SyncResult`` named tuple with status, counts, and error.
        """
        # Validate the ``since`` parameter before it enters any
        # provider-specific path.  A missing value falls back to the
        # documented 90-day default window; a malformed value (garbage,
        # wrong type, unparseable ISO string) must not crash the sync or
        # reach a connector's ``strftime`` — it becomes a controlled
        # FAILED result with an actionable, credential-free message.
        connection_id = connection_id or getattr(config, "connection_id", None)
        if selected_accounts is None:
            selected_accounts = getattr(config, "selected_accounts", None)
        try:
            _since = _resolve_since(since)
        except InvalidSinceError as exc:
            # Log only the rejection reason — never the raw value.  The
            # value may originate from user input or a stored cursor and
            # is not echoed anywhere (logs, response, SyncRun row).
            logger.error(
                "sync_since_rejected",
                provider=provider_type,
                tenant_id=self._tenant_id,
                connection_id=connection_id,
                reason=exc.reason,
                error_kind=SyncErrorKind.PERMANENT.value,
            )
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=0,
                transactions_synced=0,
                holdings_synced=0,
                unresolved_securities=0,
                error_message=str(exc),
                error_category="validation",
                error_type=type(exc).__name__,
                error_kind=SyncErrorKind.PERMANENT.value,
                duration_s=0.0,
            )
        log = logger.bind(
            provider=provider_type,
            tenant_id=self._tenant_id,
            connection_id=connection_id,
            since=_since.isoformat(),
        )
        log.info("sync_starting")

        # Persisted on the connection row so this guard survives a worker
        # restart. It also ensures an early manual retry makes no provider
        # request while Retry-After is active.
        if connection_id:
            async with self._session_factory() as guard_session:
                guarded = await guard_session.get(Credential, connection_id)
                retry_after_at = getattr(guarded, "retry_after_at", None)
                if (
                    guarded is not None
                    and str(guarded.tenant_id) == self._tenant_id
                    and retry_after_at is not None
                    and retry_after_at > datetime.now(UTC)
                ):
                    return SyncResult(
                        status=SyncRunStatus.FAILED,
                        accounts_synced=0,
                        transactions_synced=0,
                        holdings_synced=0,
                        unresolved_securities=0,
                        error_message="Provider rate limit active; retry later",
                        error_category="rate_limited",
                        retry_after_at=retry_after_at,
                        rate_limit_scope=getattr(
                            guarded, "rate_limit_scope", None
                        ),
                        rate_limit_attempts=int(
                            getattr(guarded, "rate_limit_attempts", 0) or 0
                        ),
                        duration_s=0.0,
                    )

        # Record the attempt on the connection row so the control-panel
        # UI can show per-connection status even while the run is live.
        await self._mark_connection_attempt(connection_id, log)

        connector = self._registry.get_connector(config)
        compatibility_error: str | None = None

        # A connector that is explicitly covered by the lifecycle contract
        # must not make provider calls when its installed version/capabilities
        # are incompatible.  Providers without a lifecycle entry retain the
        # legacy behaviour until their metadata is onboarded.
        lifecycle_path, matrix_path = default_contract_paths()
        lifecycle = load_json(lifecycle_path)
        contract_matrix = load_json(matrix_path)
        lifecycle_entries = cast(
            "list[dict[str, Any]]", lifecycle.get("connectors", [])
        )
        has_lifecycle_entry = any(
            item.get("name") == provider_type for item in lifecycle_entries
        )
        if has_lifecycle_entry:
            matrix_entries = cast(
                "list[dict[str, Any]]", contract_matrix.get("connectors", [])
            )
            matrix_item = next(
                (
                    item
                    for item in matrix_entries
                    if item.get("name") == provider_type
                ),
                None,
            )
            fixture_version = (
                str(matrix_item["fixture_date"])
                if matrix_item and matrix_item.get("fixture_date")
                else None
            )
            metadata = self._registry.list_connectors().get(provider_type, {})
            compatibility = evaluate_connector(
                lifecycle,
                metadata,
                fixture_version=fixture_version,
                contract_matrix=contract_matrix,
            )
            if compatibility.status == "incompatible":
                compatibility_error = (
                    f"Connector {provider_type!r} is incompatible: "
                    f"{compatibility.reason}"
                )

        # Inject persisted connector state (e.g. bunq installation material)
        # before the run so stateful connectors reuse their device identity.
        # The state is scoped per connection: two bunq connections keep
        # separate installations.
        if isinstance(connector, StatefulConnector):
            stored = await self._load_connector_state(
                provider_type, connection_id=connection_id
            )
            if stored:
                connector.set_state(stored)
                log.debug("connector_state_injected", provider=provider_type)

        # ── Run the pipeline ──────────────────────────────────────
        async with self._session_factory() as session:
            pipeline_kwargs: dict[str, Any] = {
                "resume": since is None,
                "connection_id": connection_id,
                "selected_accounts": selected_accounts,
            }
            if compatibility_error is not None:
                pipeline_kwargs["compatibility_error"] = compatibility_error
            result = await self._run_pipeline(
                session,
                connector,
                provider_type,
                _since,
                log,
                **pipeline_kwargs,
            )

        # Persist new connector state (a freshly created bunq installation)
        # regardless of run outcome — authenticate() runs first, so any
        # returned result means the installation exists server-side.
        if isinstance(connector, StatefulConnector):
            await self._persist_connector_state(
                provider_type, connector, connection_id=connection_id
            )

        await self._record_connection_outcome(
            connection_id,
            config.credentials,
            result.status,
            result.error_message,
            log,
            result=result,
        )
        metadata = self._registry.list_connectors().get(provider_type, {})
        record_connector_operation(
            provider=provider_type,
            connector_version=metadata.get("plugin_version"),
            connection_id=connection_id,
            resource="all",
            status=str(result.status),
            duration_seconds=result.duration_s or 0.0,
            error_category=result.error_category,
            retries=result.rate_limit_attempts,
            rate_limit_count=(
                1 if result.error_category == "rate_limited" else 0
            ),
            rate_limit_scope=result.rate_limit_scope,
        )

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
                error_type=result.error_type,
                error_kind=result.error_kind,
                duration_s=result.duration_s,
            )

        return result

    # ── Connector state persistence ────────────────────────────────────

    async def _load_connector_state(
        self,
        provider_key: str,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the stored state blob for ``(tenant, provider)``.

        Used to inject persistent connector state (e.g. the bunq
        installation material) into stateful connectors before a run.
        When *connection_id* is provided the state is scoped to that
        connection so two connections of the same provider keep separate
        installations; legacy rows (no connection scope) are only read
        when no connection id is given.
        """
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ConnectorState).where(
                    ConnectorState.tenant_id == self._tenant_id,
                    ConnectorState.provider_key == provider_key,
                )
            )
            # A tenant holds at most a handful of connections per
            # provider; select the row scoped to this connection in
            # Python instead of chaining dynamic SQL filters.
            row = self._connector_state_row(rows.all(), connection_id)
        state = getattr(row, "state", None) if row is not None else None
        if not isinstance(state, dict) or not state:
            return {}
        return dict(cast("dict[str, Any]", state))

    async def _persist_connector_state(
        self,
        provider_key: str,
        connector: Connector,
        connection_id: str | None = None,
    ) -> None:
        """Persist connector state (e.g. a fresh bunq installation).

        Only stateful connectors expose state; non-empty dicts are upserted
        into ``connector_state`` so the next run reuses the installation.
        When *connection_id* is provided the state row is scoped to that
        connection (per-connection installations stay isolated).
        """
        getter = getattr(connector, "get_state", None)
        state = getter() if callable(getter) else None
        if not isinstance(state, dict) or not state:
            return
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ConnectorState).where(
                    ConnectorState.tenant_id == self._tenant_id,
                    ConnectorState.provider_key == provider_key,
                )
            )
            row = self._connector_state_row(rows.all(), connection_id)
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

    @staticmethod
    def _connector_state_row(
        rows: Sequence[ConnectorState],
        connection_id: str | None,
    ) -> ConnectorState | None:
        """Find state across PostgreSQL UUID and string representations."""
        if connection_id is None:
            return next(
                (row for row in rows if row.connection_id is None),
                None,
            )
        target = str(connection_id)
        return next(
            (
                row
                for row in rows
                if row.connection_id is not None
                and str(row.connection_id) == target
            ),
            None,
        )

    # ── Bunq cards / scheduled payments sync ──────────────────────────

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
        compatibility_error: str | None = None,
    ) -> SyncResult:
        from datetime import datetime as _dt

        start_ts = _dt.now(UTC)
        context = SyncContext(
            tenant_id=self._tenant_id,
            provider_type=provider_type,
            since=since,
            connection_id=connection_id,
        )
        from finance_sync.db.uow import UnitOfWork as _UnitOfWork

        uow = _UnitOfWork(session)
        run = None
        run_id: str | None = None
        accounts_synced = 0
        transactions_synced = 0
        holdings_synced = 0
        unresolved_keys: set[str] = set()
        transaction_report: dict[str, int] = {}
        current_operation = "start_sync_run"
        current_account_id: str | None = None

        # Account selection: when the connection pins a set of provider
        # account ids, only those accounts are synced (and their
        # transactions/holdings fetched).  NULL/empty means "all".
        selected_set: set[str] | None = (
            set(selected_accounts) if selected_accounts else None
        )
        if selected_set is not None:
            log.debug(
                "account_selection_filter",
                selected=len(selected_set),
            )

        try:
            async with uow:
                persistence = SyncPersistence(
                    self,
                    context=PersistenceContext(
                        tenant_id=self._tenant_id,
                        provider_type=provider_type,
                        connection_id=connection_id,
                    ),
                )
                # 1. SyncRun record
                run = await start_sync_run(
                    uow,
                    connector=provider_type,
                    connection_id=connection_id,
                )
                run_id = str(run.id)
                log = log.bind(sync_run_id=run_id)

                if compatibility_error:
                    raise PermanentError(compatibility_error)

                # 2. Authenticate
                current_operation = "authenticate"
                await connector.authenticate()
                log.debug("authenticated")

                # 3. Fetch + upsert accounts through an isolated stage.
                current_operation = "fetch_accounts"
                account_result = await AccountSyncStage(persistence).run(
                    uow,
                    connector,
                    selected_accounts=selected_accounts,
                    connection_id=context.connection_id,
                    persist=False,
                )
                canonical_accounts = account_result.accounts
                supports_holdings = account_result.supports_holdings
                accounts_synced = len(canonical_accounts)
                log.debug("accounts_fetched", count=accounts_synced)

                # A configured account selection is an import contract, not
                # merely a best-effort filter.  If the provider no longer
                # returns any selected account (for example after an account
                # id changed), completing here would stamp the connection as
                # successful while writing no data at all.
                if selected_set is not None and not canonical_accounts:
                    selection_error = (
                        "Account selection validation failed: "
                        "none of the selected accounts was returned by the "
                        "provider"
                    )
                    raise PermanentError(selection_error)

                # Commit the run and account rows before processing resources.
                # Resource writes are isolated below, one transaction per
                # account, so a failed account cannot roll back a previously
                # completed account (or leave this account partially written).
                await uow.commit()

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
                    current_account_id = ca.external_account_id
                    acct_since = cursors.get(ca.external_account_id, since)
                    current_operation = "fetch_transactions"
                    raw_txns = await connector._rate_limited_fetch_transactions(  # type: ignore[attr-defined]
                        acct_since, account_id=ca.external_account_id
                    )
                    canonical_txns = connector.transform_transactions(raw_txns)

                    # All transaction and holding upserts for this account,
                    # including security resolution and its sync cursor, must
                    # share one commit boundary.  The UoW rolls this batch
                    # back automatically when any write raises.
                    async with _UnitOfWork(session) as account_uow:
                        # Persisting the account is part of this account's
                        # transaction.  Reuse the returned entity for the FK;
                        # querying again would be redundant.
                        current_operation = "persist_account"
                        acct = await persistence.persist_account(
                            account_uow,
                            ca,
                            connection_id=connection_id,
                        )
                        account_id = str(getattr(acct, "id", ""))
                        if not account_id:
                            log.warning(
                                "account_persistence_returned_no_id",
                                external_account_id=ca.external_account_id,
                            )
                            continue
                        await persistence.persist_cash_balances(
                            account_uow, account_id, ca
                        )
                        current_operation = "persist_transactions"
                        transaction_result = await TransactionSyncStage(
                            persistence
                        ).run(
                            account_uow,
                            canonical_txns,
                            account_id=account_id,
                            provider_type=provider_type,
                            connection_id=connection_id,
                        )
                        account_transactions = transaction_result.count
                        account_unresolved = set(
                            transaction_result.unresolved_keys
                        )

                        account_holdings = 0
                        account_holdings_unresolved: set[str] = set()
                        if supports_holdings:
                            current_operation = "fetch_holdings"
                            raw_holdings = (
                                await connector._rate_limited_fetch_holdings(  # type: ignore[attr-defined]
                                    account_id=ca.external_account_id
                                )
                            )
                            canonical_holdings = connector.transform_holdings(
                                raw_holdings
                            )
                            current_operation = "persist_holdings"
                            holdings_result = await HoldingsSyncStage(
                                persistence
                            ).run(
                                account_uow,
                                canonical_holdings,
                                account_id=account_id,
                                provider_key=provider_type,
                            )
                            account_holdings = holdings_result.count
                            account_holdings_unresolved.update(
                                holdings_result.unresolved_keys
                            )

                        current_operation = "persist_sync_cursor"
                        await upsert_sync_cursor(
                            account_uow.session,
                            tenant_id=self._tenant_id,
                            connector=provider_type,
                            resource=ca.external_account_id,
                            cursor=start_ts,
                            connection_id=connection_id,
                        )
                    transactions_synced += account_transactions
                    transaction_result.add_to_report(transaction_report)
                    holdings_synced += account_holdings
                    unresolved_keys.update(account_unresolved)
                    unresolved_keys.update(account_holdings_unresolved)
                log.debug("transactions_fetched", count=transactions_synced)
                await complete_sync_run(
                    uow,
                    run,
                    status=SyncRunStatus.COMPLETED,
                    items_processed=(
                        accounts_synced + transactions_synced + holdings_synced
                    ),
                    report={
                        **transaction_report,
                        "accounts": accounts_synced,
                        "transactions": transactions_synced,
                        "holdings": holdings_synced,
                        "unresolved": len(unresolved_keys),
                    },
                    cursor=start_ts,
                )
                if supports_holdings or unresolved_keys:
                    await outbox_sync_completed(
                        uow,
                        tenant_id=self._tenant_id,
                        run_id=str(run.id),
                        provider_key=provider_type,
                        accounts=accounts_synced,
                        transactions=transactions_synced,
                        holdings=holdings_synced,
                        unresolved_securities=len(unresolved_keys),
                    )
                # The outer UoW committed before the account loop and marks
                # itself as committed, so explicitly persist this final run
                # status/event transaction.
                await session.commit()
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
            capture_connector_exception(
                exc,
                connector=provider_type,
                operation=current_operation,
                connection_id=connection_id,
                provider_account_id=current_account_id,
                correlation_id=run_id,
            )
            end_ts = _dt.now(UTC)
            await self._mark_run_failed(
                session,
                run,
                str(exc),
                log,
                error_category=categorize_sync_error(exc),
                connection_id=connection_id,
            )
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
                error_message=str(exc),
                error_type=type(exc).__name__,
                error_category=categorize_sync_error(exc),
                error_kind=classify_sync_error(exc).value,
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        except RateLimitError as exc:
            capture_connector_exception(
                exc,
                connector=provider_type,
                operation=current_operation,
                connection_id=connection_id,
                provider_account_id=current_account_id,
                correlation_id=run_id,
            )
            end_ts = _dt.now(UTC)
            retry_after_at = (
                end_ts + timedelta(seconds=exc.retry_after)
                if exc.retry_after is not None
                else None
            )
            category = categorize_sync_error(exc)
            await self._mark_run_failed(
                session,
                run,
                str(exc),
                log,
                error_category=category,
                connection_id=connection_id,
                retry_after_at=retry_after_at,
                rate_limit_attempts=1,
                rate_limit_scope="connection",
                last_http_status=429,
            )
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
                error_message=str(exc),
                error_category=category,
                error_type=type(exc).__name__,
                error_kind=classify_sync_error(exc).value,
                retry_after_at=retry_after_at,
                rate_limit_scope="connection",
                rate_limit_attempts=1,
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        except (TransientError, ConnectorError) as exc:
            capture_connector_exception(
                exc,
                connector=provider_type,
                operation=current_operation,
                connection_id=connection_id,
                provider_account_id=current_account_id,
                correlation_id=run_id,
            )
            end_ts = _dt.now(UTC)
            await self._mark_run_failed(
                session,
                run,
                str(exc),
                log,
                error_category=categorize_sync_error(exc),
                connection_id=connection_id,
            )
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
                error_message=str(exc),
                error_category=categorize_sync_error(exc),
                error_type=type(exc).__name__,
                error_kind=classify_sync_error(exc).value,
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        except Exception as exc:
            capture_connector_exception(
                exc,
                connector=provider_type,
                operation=current_operation,
                connection_id=connection_id,
                provider_account_id=current_account_id,
                correlation_id=run_id,
            )
            end_ts = _dt.now(UTC)
            error_message = safe_sync_error_message(exc)
            log.error(
                "sync_pipeline_failed",
                error_type=type(exc).__name__,
                error_category=categorize_sync_error(exc),
                error_kind=classify_sync_error(exc).value,
                error=str(exc)[:500],
            )
            await self._mark_run_failed(
                session,
                run,
                error_message,
                log,
                error_category=categorize_sync_error(exc),
                connection_id=connection_id,
            )
            return SyncResult(
                status=SyncRunStatus.FAILED,
                accounts_synced=accounts_synced,
                transactions_synced=transactions_synced,
                holdings_synced=holdings_synced,
                unresolved_securities=len(unresolved_keys),
                error_message=error_message,
                error_type=type(exc).__name__,
                error_category=categorize_sync_error(exc),
                error_kind=classify_sync_error(exc).value,
                duration_s=(end_ts - start_ts).total_seconds(),
            )

    # ── Cards pipeline ─────────────────────────────────────────────

    # ── Entity upsert helpers ──────────────────────────────────────

    # ── Failure handling ───────────────────────────────────────────

    async def _mark_run_failed(
        self,
        session: AsyncSession,
        run: object | None,
        error_message: str,
        log: structlog.BoundLogger,
        *,
        connection_id: str | None = None,
        error_category: str = "unknown",
        retry_after_at: datetime | None = None,
        rate_limit_attempts: int = 0,
        rate_limit_scope: str | None = None,
        last_http_status: int | None = None,
    ) -> None:
        """Persist a failed SyncRun outside the main UoW (which rolled back).

        The in-flight ``SyncRun`` row was rolled back with the transaction,
        so it cannot be reloaded — instead a fresh ``FAILED`` row is
        inserted so failed runs stay observable (alerting relies on them).
        The row carries the run's *connection_id* when the failed run was
        connection-scoped.
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
                        error_category=error_category,
                        retry_after_at=retry_after_at,
                        rate_limit_attempts=rate_limit_attempts,
                        rate_limit_scope=rate_limit_scope,
                        last_http_status=last_http_status,
                    )
                else:
                    # The original row never made it to the DB — insert a
                    # fresh FAILED record so the failure is observable.
                    connector = getattr(run, "connector", None) or "unknown"
                    uow.session.add(
                        _SyncRun(
                            connector=connector,
                            connection_id=connection_id,
                            status=SyncRunStatus.FAILED,
                            completed_at=datetime.now(UTC),
                            error_message=error_message[:2048],
                            error_category=error_category,
                            retry_after_at=retry_after_at,
                            rate_limit_attempts=rate_limit_attempts,
                            rate_limit_scope=rate_limit_scope,
                            last_http_status=last_http_status,
                        )
                    )
        except Exception as exc:
            log.error(
                "failed_to_persist_failed_sync_run",
                error=str(exc),
            )


def _default_since() -> dt_type:
    """Return a default ``since`` date of 90 days ago."""
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=90)


def _resolve_since(
    since: dt_type | str | None,
) -> dt_type:
    """Normalise the ``run_sync`` ``since`` parameter.

    Delegates to :func:`validate_since` with the orchestrator's documented
    default (the 90-day backfill window).  A rejected value raises
    :class:`InvalidSinceError` — the caller (``run_sync``) converts it
    into a controlled ``FAILED`` ``SyncResult`` so no unhandled exception
    escapes to the API layer.
    """
    return validate_since(since, default=_default_since())
