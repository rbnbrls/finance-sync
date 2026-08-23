"""Bunq cards/scheduled-payment sync pipeline mixin."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import structlog

from finance_sync.connectors.exceptions import (
    ConnectorError,
    PermanentError,
    TransientError,
)
from finance_sync.models.enums import SyncRunStatus
from finance_sync.sync.persistence import PersistenceContext, SyncPersistence
from finance_sync.sync.sync_cursor import (
    RESOURCE_CARD_TRANSACTIONS,
    get_cursor,
    upsert_sync_cursor,
)
from finance_sync.sync.sync_run import complete_sync_run, start_sync_run

if TYPE_CHECKING:
    from datetime import datetime as dt_type

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.connectors.base import Connector
    from finance_sync.connectors.models import (
        ConnectorConfig,
        RawCardTransaction,
        RawScheduledPayment,
    )

logger = structlog.get_logger("finance_sync.sync.cards_pipeline")


class _CardsConnector(Protocol):
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
class StatefulConnector(Protocol):
    def set_state(self, state: dict[str, object]) -> None: ...

    def get_state(self) -> dict[str, object]: ...


def _default_since() -> dt_type:
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=90)


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


class CardsSyncMixin:
    """Reusable cards pipeline; the host supplies orchestration callbacks."""

    async def run_bunq_cards_sync(
        self: Any,
        config: ConnectorConfig,
        *,
        since: dt_type | None = None,
        connection_id: str | None = None,
        selected_accounts: list[str] | None = None,
    ) -> BunqCardsSyncResult:
        """Fetch scheduled payments + card transactions and upsert them.

        Runs as an independent sync cycle (connector ``bunq_cards``) so
        the hourly cards/schedules cadence does not depend on the main
        15-minute transaction sync.  Upserts are idempotent: both tables
        carry a ``(tenant_id, provider_key, connection_id, external_*)``
        unique constraint, so re-runs update in place instead of
        duplicating.

        Args:
            config: ``ConnectorConfig`` with credentials + options.
            since:  Only fetch card transactions on or after this time.
                    Defaults to the stored cards cursor, or 90 days ago
                    for the first sync.  Scheduled payments are always
                    fetched in full (they are templates, not an
                    append-only stream).
            connection_id: Stable connection id the run belongs to;
                    scopes cursors, runs and connector state per
                    connection and updates the connection's
                    ``last_attempt_at`` / ``last_success_at`` /
                    ``last_error`` fields.
            selected_accounts: Provider account ids to sync.  When
                    provided, scheduled payments are filtered to those
                    accounts.  Card transactions are card-scoped (not
                    account-scoped) and are always synced for the
                    connection.

        Returns:
            A ``BunqCardsSyncResult`` with status, counts, and error.
        """
        # Resume from the stored cards cursor unless an explicit window
        # was given (explicit backfills always win).  The cursor is
        # scoped per connection so two bunq connections resume
        # independently.
        cursor = None
        if since is None:
            async with self._session_factory() as session:
                cursor = await get_cursor(
                    session,
                    tenant_id=self._tenant_id,
                    connector="bunq_cards",
                    resource=RESOURCE_CARD_TRANSACTIONS,
                    connection_id=connection_id,
                )
        _since = since or cursor or _default_since()
        log = logger.bind(
            provider="bunq",
            tenant_id=self._tenant_id,
            connection_id=connection_id,
            since=_since.isoformat(),
        )
        log.info("bunq_cards_sync_starting")

        await self._mark_connection_attempt(connection_id, log)

        connector = self._registry.get_connector(config)

        # Reuse the persisted bunq installation across cards syncs too —
        # scoped per connection so two bunq connections keep separate
        # device installations.
        if isinstance(connector, StatefulConnector):
            stored = await self._load_connector_state(
                "bunq", connection_id=connection_id
            )
            if stored:
                connector.set_state(stored)

        async with self._session_factory() as session:
            result = await self._run_cards_pipeline(
                session,
                connector,
                _since,
                log,
                connection_id=connection_id,
                selected_accounts=selected_accounts,
            )

        if isinstance(connector, StatefulConnector):
            await self._persist_connector_state(
                "bunq", connector, connection_id=connection_id
            )

        await self._record_connection_outcome(
            connection_id,
            config.credentials,
            result.status,
            result.error_message,
            log,
        )

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

    async def _run_cards_pipeline(
        self: Any,
        session: AsyncSession,
        connector: Connector,
        since: dt_type,
        log: structlog.BoundLogger,
        *,
        connection_id: str | None = None,
        selected_accounts: list[str] | None = None,
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
        persistence = SyncPersistence(
            self,
            context=PersistenceContext(
                tenant_id=self._tenant_id,
                provider_type="bunq_cards",
                connection_id=connection_id,
            ),
        )
        cards_connector = cast(_CardsConnector, connector)
        run = None
        schedules_synced = 0
        card_txns_synced = 0

        # Account selection filters the account-scoped schedule stream;
        # card transactions are card-scoped (external_account_id is the
        # card id, not an account id) and stay connection-wide so user
        # card activity is never silently dropped by an account filter.
        selected_set: set[str] | None = (
            set(selected_accounts) if selected_accounts else None
        )

        try:
            async with uow:
                # 1. SyncRun record
                run = await start_sync_run(
                    uow,
                    connector="bunq_cards",
                    connection_id=connection_id,
                )
                log = log.bind(sync_run_id=str(run.id))

                # 2. Authenticate
                await connector.authenticate()
                log.debug("authenticated")

                # 3. Fetch + upsert accounts so schedules can resolve
                #    their account FK (mirrors the main pipeline).
                raw_accounts = await connector._rate_limited_fetch_accounts()  # type: ignore[attr-defined]
                canonical_accounts = connector.transform_accounts(raw_accounts)
                if selected_set is not None:
                    canonical_accounts = [
                        ca
                        for ca in canonical_accounts
                        if ca.external_account_id in selected_set
                    ]
                for ca in canonical_accounts:
                    await persistence.persist_account(
                        uow, ca, connection_id=connection_id
                    )
                log.debug("accounts_fetched", count=len(canonical_accounts))

                # 4. Scheduled payments (full fetch — templates, not a
                #    since-filtered stream).  Filtered to the selected
                #    accounts when the connection pins a selection.
                raw_schedules = await cards_connector.fetch_scheduled_payments()
                canonical_schedules = connector.transform_scheduled_payments(
                    raw_schedules
                )
                if selected_set is not None:
                    canonical_schedules = [
                        cs
                        for cs in canonical_schedules
                        if cs.external_account_id in selected_set
                    ]
                for cs in canonical_schedules:
                    acct = await uow.accounts.get_by_external_id(
                        self._tenant_id,
                        cs.provider_key,
                        cs.external_account_id,
                        connection_id=connection_id,
                    )
                    if acct is None:
                        log.warning(
                            "account_not_found_for_schedule",
                            external_account_id=cs.external_account_id,
                        )
                        continue
                    await persistence.persist_scheduled_payment(
                        uow, cs, acct.id, connection_id=connection_id
                    )
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
                    await persistence.persist_card_transaction(
                        uow, cct, connection_id=connection_id
                    )
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
                    connection_id=connection_id,
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
            await self._mark_run_failed(
                session, run, str(exc), log, connection_id=connection_id
            )
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
            await self._mark_run_failed(
                session, run, tb, log, connection_id=connection_id
            )
            return BunqCardsSyncResult(
                status=SyncRunStatus.FAILED,
                schedules_synced=schedules_synced,
                card_transactions_synced=card_txns_synced,
                error_message=tb,
                duration_s=(end_ts - start_ts).total_seconds(),
            )
