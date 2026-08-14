"""Tests for the SyncOrchestrator.

Uses MockConnector from conftest and patches the UnitOfWork / repositories
so we can test the orchestration logic without a real database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, String
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalCardTransactionData,
    CanonicalScheduledPaymentData,
    CanonicalTransactionData,
)
from finance_sync.models.enums import (
    ReconciliationRunStatus,
    SyncRunStatus,
)
from finance_sync.sync.orchestrator import (
    ReconciliationRunSummary,
    SyncOrchestrator,
    SyncResult,
)

# ── Test model for SyncRun (SQLite-compatible) ────────────────────

_sync_run_meta = MetaData()
SyncRunTestBase = declarative_base(metadata=_sync_run_meta)


class SyncRunTestModel(SyncRunTestBase):
    """SyncRun model adapted for SQLite (no JSONB)."""

    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running"
    )
    started_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    items_processed: Mapped[int | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(nullable=True)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def sample_account_data() -> CanonicalAccountData:
    """Return a canonical account for testing."""
    return CanonicalAccountData(
        provider_key="mock_provider",
        external_account_id="ext_acc_1",
        name="Test Checking",
        account_type="checking",
        currency_code="EUR",
        current_balance=Decimal("1500.00"),
        available_balance=Decimal("1400.00"),
    )


@pytest.fixture
def sample_transaction_data() -> CanonicalTransactionData:
    """Return a canonical transaction for testing."""
    return CanonicalTransactionData(
        provider_key="mock_provider",
        external_transaction_id="ext_txn_1",
        external_account_id="ext_acc_1",
        amount=Decimal("-42.50"),
        currency_code="EUR",
        occurred_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
        booked_at=datetime(2025, 6, 1, 14, 0, 0, tzinfo=UTC),
        transaction_type="purchase",
        description="Coffee",
        status="booked",
    )


# ── SyncResult tests ──────────────────────────────────────────────


class TestSyncResult:
    """SyncResult is a simple data holder."""

    def test_construct_and_repr(self) -> None:
        result = SyncResult(
            status=SyncRunStatus.COMPLETED,
            accounts_synced=5,
            transactions_synced=42,
            error_message=None,
            duration_s=1.5,
        )
        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 5
        assert result.transactions_synced == 42
        assert result.error_message is None
        assert result.duration_s == 1.5
        assert "SyncResult" in repr(result)
        assert "completed" in repr(result)

    def test_failed_result(self) -> None:
        result = SyncResult(
            status=SyncRunStatus.FAILED,
            accounts_synced=0,
            transactions_synced=0,
            error_message="Auth failed",
            duration_s=0.5,
        )
        assert result.status == SyncRunStatus.FAILED
        assert result.error_message == "Auth failed"


class TestReconciliationRunSummary:
    """ReconciliationRunSummary is a simple data holder."""

    def test_construct_and_repr(self) -> None:
        summary = ReconciliationRunSummary(
            run_id="rec_run_1",
            status=ReconciliationRunStatus.COMPLETED,
            finding_count=5,
        )
        assert summary.run_id == "rec_run_1"
        assert summary.status == ReconciliationRunStatus.COMPLETED
        assert summary.finding_count == 5
        assert "ReconciliationRunSummary" in repr(summary)
        assert "completed" in repr(summary)

    def test_no_findings(self) -> None:
        summary = ReconciliationRunSummary(
            run_id="rec_run_2",
            status=ReconciliationRunStatus.COMPLETED,
            finding_count=0,
        )
        assert summary.finding_count == 0

    def test_failed_run(self) -> None:
        summary = ReconciliationRunSummary(
            run_id="rec_run_3",
            status=ReconciliationRunStatus.FAILED,
            finding_count=0,
        )
        assert summary.status == ReconciliationRunStatus.FAILED


# ── Orchestrator tests (mocked UoW) ───────────────────────────────


class TestSyncOrchestratorInit:
    """Orchestrator stores dependencies correctly."""

    def test_constructor(self) -> None:
        session_factory = MagicMock()
        registry = MagicMock()
        orchestrator = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_1",
        )
        assert orchestrator._tenant_id == "tenant_1"
        assert orchestrator._settings is None

    def test_constructor_with_settings(self) -> None:
        """Settings object is stored when provided."""
        session_factory = MagicMock()
        registry = MagicMock()
        settings = MagicMock()
        settings.worker_job_reconciliation_after_sync_enabled = False
        orchestrator = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_1",
            settings=settings,
        )
        assert orchestrator._settings is settings

    def test_reconciliation_after_sync_enabled_default(self) -> None:
        """Default (no settings) returns True."""
        orchestrator = SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )
        assert orchestrator._reconciliation_after_sync_enabled is True

    def test_reconciliation_after_sync_enabled_when_setting_true(self) -> None:
        """Returns True when setting is True."""
        settings = MagicMock()
        settings.worker_job_reconciliation_after_sync_enabled = True
        orchestrator = SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
            settings=settings,
        )
        assert orchestrator._reconciliation_after_sync_enabled is True

    def test_reconciliation_after_sync_enabled_when_setting_false(self) -> None:
        """Returns False when setting is False."""
        settings = MagicMock()
        settings.worker_job_reconciliation_after_sync_enabled = False
        orchestrator = SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
            settings=settings,
        )
        assert orchestrator._reconciliation_after_sync_enabled is False


class TestSyncOrchestratorRunReconciliation:
    """Test the run_reconciliation method with mocked dependencies."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        session_factory = MagicMock()  # Must be MagicMock, not AsyncMock,
        # so that session_factory() returns an object with __aenter__/__aexit__
        # set up properly for the outbox async context manager.
        mock_session = AsyncMock()
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
        registry = MagicMock()
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_test_1",
        )

    async def test_run_reconciliation_completed(self, orchestrator) -> None:
        """Successful reconciliation returns summary with finding count."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = "rec_run_101"
        mock_run.status = ReconciliationRunStatus.COMPLETED
        mock_run.finding_count = 3
        mock_run.summary = {"by_kind": {"duplicate": 2, "missing": 1}}

        with (
            patch(
                "finance_sync.services.reconciliation.ReconciliationService.reconcile",
                new=AsyncMock(return_value=mock_run),
            ),
            patch(
                "finance_sync.db.uow.UnitOfWork",
            ) as mock_uow_cls,
            patch(
                "finance_sync.sync.orchestrator.outbox_reconciliation_completed",
                new=AsyncMock(),
            ) as mock_outbox,
        ):
            mock_uow = MagicMock()
            mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
            mock_uow.__aexit__ = AsyncMock(return_value=None)
            mock_uow_cls.return_value = mock_uow

            summary = await orchestrator.run_reconciliation()

        assert summary.run_id == "rec_run_101"
        assert summary.status == ReconciliationRunStatus.COMPLETED
        assert summary.finding_count == 3

        # Verify outbox message was emitted with correct details
        mock_outbox.assert_awaited_once()
        outbox_kwargs = mock_outbox.call_args.kwargs
        assert outbox_kwargs["run_id"] == "rec_run_101"
        assert outbox_kwargs["finding_count"] == 3

    async def test_run_reconciliation_emits_outbox(self, orchestrator) -> None:
        """Outbox message is emitted on successful reconciliation."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = "rec_run_outbox"
        mock_run.status = ReconciliationRunStatus.COMPLETED
        mock_run.finding_count = 5
        mock_run.summary = {
            "by_kind": {"duplicate_transaction": 5},
            "by_severity": {"warning": 5},
        }

        with (
            patch(
                "finance_sync.services.reconciliation.ReconciliationService.reconcile",
                new=AsyncMock(return_value=mock_run),
            ),
            patch(
                "finance_sync.db.uow.UnitOfWork",
            ) as mock_uow_cls,
            patch(
                "finance_sync.sync.orchestrator.outbox_reconciliation_completed",
                new=AsyncMock(),
            ) as mock_outbox,
        ):
            mock_uow = MagicMock()
            mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
            mock_uow.__aexit__ = AsyncMock(return_value=None)
            mock_uow_cls.return_value = mock_uow

            await orchestrator.run_reconciliation()
            mock_outbox.assert_awaited_once()

    async def test_run_reconciliation_skips_outbox_on_failure(
        self, orchestrator
    ) -> None:
        """No outbox message when reconciliation fails."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = "rec_run_fail"
        mock_run.status = ReconciliationRunStatus.FAILED
        mock_run.finding_count = 0
        mock_run.summary = None

        with (
            patch(
                "finance_sync.services.reconciliation.ReconciliationService.reconcile",
                new=AsyncMock(return_value=mock_run),
            ),
            patch(
                "finance_sync.sync.orchestrator.outbox_reconciliation_completed",
                new=AsyncMock(),
            ) as mock_outbox,
        ):
            summary = await orchestrator.run_reconciliation()

        assert summary.status == ReconciliationRunStatus.FAILED
        mock_outbox.assert_not_awaited()

    async def test_run_reconciliation_outbox_failure_does_not_crash(
        self, orchestrator
    ) -> None:
        """Outbox failure is caught and logged; the run summary is still returned."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = "rec_run_outbox_fail"
        mock_run.status = ReconciliationRunStatus.COMPLETED
        mock_run.finding_count = 2
        mock_run.summary = {"by_kind": {}}

        with (
            patch(
                "finance_sync.services.reconciliation.ReconciliationService.reconcile",
                new=AsyncMock(return_value=mock_run),
            ),
            patch(
                "finance_sync.db.uow.UnitOfWork",
            ) as mock_uow_cls,
            patch(
                "finance_sync.sync.orchestrator.outbox_reconciliation_completed",
                new=AsyncMock(side_effect=RuntimeError("Outbox DB error")),
            ),
        ):
            mock_uow = MagicMock()
            mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
            mock_uow.__aexit__ = AsyncMock(return_value=None)
            mock_uow_cls.return_value = mock_uow

            # Should not raise — outbox failure is caught and logged
            summary = await orchestrator.run_reconciliation()

        assert summary.run_id == "rec_run_outbox_fail"
        assert summary.status == ReconciliationRunStatus.COMPLETED

    async def test_run_reconciliation_failed(self, orchestrator) -> None:
        """Failed reconciliation returns FAILED status."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = "rec_run_102"
        mock_run.status = ReconciliationRunStatus.FAILED
        mock_run.finding_count = 0
        mock_run.summary = None

        with patch(
            "finance_sync.services.reconciliation.ReconciliationService.reconcile",
            new=AsyncMock(return_value=mock_run),
        ):
            summary = await orchestrator.run_reconciliation()

        assert summary.run_id == "rec_run_102"
        assert summary.status == ReconciliationRunStatus.FAILED
        assert summary.finding_count == 0


class TestSyncOrchestratorRunPipeline:
    """Test the pipeline with a mocked UnitOfWork."""

    @pytest.fixture
    async def orchestrator(self) -> SyncOrchestrator:
        session_factory = AsyncMock()
        registry = MagicMock()
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_1",
        )

    @pytest.fixture
    def mock_connector(self, sample_account_data, sample_transaction_data):
        """Create a fully working mock connector."""
        connector = MagicMock()
        connector.name = "mock_provider"

        connector.authenticate = AsyncMock()

        connector._rate_limited_fetch_accounts = AsyncMock(
            return_value=[sample_account_data]
        )
        connector.transform_accounts = MagicMock(
            return_value=[sample_account_data]
        )
        connector.transform_transactions = MagicMock(
            return_value=[sample_transaction_data]
        )

        async def fetch_txns(since, account_id=None, limit=None):
            return [sample_transaction_data]

        connector._rate_limited_fetch_transactions = AsyncMock(
            side_effect=fetch_txns
        )

        return connector

    @pytest.fixture
    def mock_uow(self):
        """Create a UnitOfWork with mocked repositories."""
        uow = MagicMock()

        session = AsyncMock()
        uow.session = session

        # Accounts repo: return a valid account for the transaction phase
        existing_account = MagicMock()
        existing_account.id = "acct_uuid_1"

        accounts_repo = AsyncMock()
        accounts_repo.get_by_external_id = AsyncMock(
            side_effect=[None, existing_account]
        )
        uow.accounts = accounts_repo

        txn_repo = AsyncMock()
        txn_repo.get_by_external_id = AsyncMock(return_value=None)
        uow.transactions = txn_repo

        sync_runs_repo = AsyncMock()
        uow.sync_runs = sync_runs_repo

        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=None)
        uow.commit = AsyncMock()
        uow.rollback = AsyncMock()

        return uow

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_full_pipeline(
        self,
        mock_complete_run,
        mock_start_run,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """Full pipeline completes successfully with mocked UoW."""
        mock_run = MagicMock(id="run_1")
        mock_run.id = "run_1"
        mock_start_run.return_value = mock_run
        mock_complete_run.return_value = mock_run

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                provider_type="mock_provider",
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1
        assert result.transactions_synced >= 1
        assert result.error_message is None

        mock_connector.authenticate.assert_awaited_once()

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    async def test_permanent_error_handling(
        self,
        mock_start_run,
        orchestrator,
        mock_uow,
    ) -> None:
        """PermanentError during sync marks run as failed."""
        from finance_sync.connectors.exceptions import PermanentError

        mock_run = MagicMock(id="run_1")
        mock_run.id = "run_1"
        mock_start_run.return_value = mock_run

        connector = MagicMock()
        connector.authenticate = AsyncMock(
            side_effect=PermanentError("Bad credentials")
        )
        connector.name = "mock_provider"

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=connector,
                provider_type="mock_provider",
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.FAILED
        assert "Bad credentials" in (result.error_message or "")

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    async def test_transient_error_handling(
        self,
        mock_start_run,
        orchestrator,
        mock_uow,
    ) -> None:
        """TransientError during sync marks run as failed."""
        from finance_sync.connectors.exceptions import TransientError

        mock_run = MagicMock(id="run_2")
        mock_run.id = "run_2"
        mock_start_run.return_value = mock_run

        connector = MagicMock()
        connector.authenticate = AsyncMock()
        connector.name = "mock_provider"

        connector._rate_limited_fetch_accounts = AsyncMock(
            side_effect=TransientError("Provider unavailable")
        )

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=connector,
                provider_type="mock_provider",
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.FAILED
        assert "Provider unavailable" in (result.error_message or "")


class TestSyncCursorResume:
    """Sync cursor persistence (G-03): first-sync, resume, idempotency.

    Uses the same mocked UoW / connector fixtures as
    ``TestSyncOrchestratorRunPipeline``.
    """

    @pytest.fixture
    async def orchestrator(self) -> SyncOrchestrator:
        session_factory = AsyncMock()
        registry = MagicMock()
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_1",
        )

    @pytest.fixture
    def mock_connector(self, sample_account_data, sample_transaction_data):
        """Create a fully working mock connector."""
        connector = MagicMock()
        connector.name = "mock_provider"

        connector.authenticate = AsyncMock()

        connector._rate_limited_fetch_accounts = AsyncMock(
            return_value=[sample_account_data]
        )
        connector.transform_accounts = MagicMock(
            return_value=[sample_account_data]
        )
        connector.transform_transactions = MagicMock(
            return_value=[sample_transaction_data]
        )

        async def fetch_txns(since, account_id=None, limit=None):
            return [sample_transaction_data]

        connector._rate_limited_fetch_transactions = AsyncMock(
            side_effect=fetch_txns
        )

        return connector

    @pytest.fixture
    def mock_uow(self):
        """Create a UnitOfWork with mocked repositories."""
        uow = MagicMock()

        session = AsyncMock()
        uow.session = session

        existing_account = MagicMock()
        existing_account.id = "acct_uuid_1"

        accounts_repo = AsyncMock()
        accounts_repo.get_by_external_id = AsyncMock(
            side_effect=[None, existing_account]
        )
        uow.accounts = accounts_repo

        txn_repo = AsyncMock()
        txn_repo.get_by_external_id = AsyncMock(return_value=None)
        uow.transactions = txn_repo

        sync_runs_repo = AsyncMock()
        uow.sync_runs = sync_runs_repo

        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=None)
        uow.commit = AsyncMock()
        uow.rollback = AsyncMock()

        return uow

    @patch("finance_sync.sync.orchestrator.get_connector_cursors")
    @patch("finance_sync.sync.orchestrator.upsert_sync_cursor")
    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_first_sync_uses_default_window(
        self,
        mock_complete_run,
        mock_start_run,
        mock_upsert_cursor,
        mock_get_cursors,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """No stored cursors → fetch from the 90-day default window."""
        mock_get_cursors.return_value = {}
        mock_start_run.return_value = MagicMock(id="run_cursor_1")
        default_since = datetime.now(UTC) - timedelta(days=90)

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                provider_type="mock_provider",
                since=default_since,
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        # Fetched the full default window, not a stored cursor
        call = mock_connector._rate_limited_fetch_transactions.await_args
        assert call.args[0] == default_since

        # On success the cursor is persisted (run start watermark) and
        # the SyncRun record exposes it.
        upsert_kwargs = mock_upsert_cursor.await_args.kwargs
        assert upsert_kwargs["resource"] == "ext_acc_1"
        assert upsert_kwargs["cursor"] is not None
        complete_kwargs = mock_complete_run.await_args.kwargs
        assert complete_kwargs["cursor"] is not None

    @patch("finance_sync.sync.orchestrator.get_connector_cursors")
    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_resume_uses_stored_cursor(
        self,
        mock_complete_run,
        mock_start_run,
        mock_get_cursors,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """A re-run resumes from the stored cursor, not the full window."""
        stored = datetime.now(UTC) - timedelta(days=2)
        mock_get_cursors.return_value = {"ext_acc_1": stored}
        mock_start_run.return_value = MagicMock(id="run_cursor_2")
        default_since = datetime.now(UTC) - timedelta(days=90)

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                provider_type="mock_provider",
                since=default_since,
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        call = mock_connector._rate_limited_fetch_transactions.await_args
        assert call.args[0] == stored
        assert call.args[0] != default_since

    @patch("finance_sync.sync.orchestrator.get_connector_cursors")
    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_mixed_new_and_resumed_accounts(
        self,
        mock_complete_run,
        mock_start_run,
        mock_get_cursors,
        orchestrator,
        mock_connector,
        mock_uow,
        sample_account_data,
    ) -> None:
        """Accounts without a cursor keep the default; resumed ones don't."""
        second_account = CanonicalAccountData(
            provider_key="mock_provider",
            external_account_id="ext_acc_2",
            name="Savings",
            account_type="savings",
            currency_code="EUR",
            current_balance=Decimal("500.00"),
            available_balance=Decimal("500.00"),
        )
        mock_connector.transform_accounts.return_value = [
            sample_account_data,
            second_account,
        ]
        mock_connector._rate_limited_fetch_transactions = AsyncMock(
            return_value=[]
        )

        async def get_acct(tenant_id, provider_key, external_account_id):
            acct = MagicMock()
            acct.id = f"acct_{external_account_id}"
            return acct

        mock_uow.accounts.get_by_external_id = AsyncMock(
            side_effect=get_acct
        )

        stored = datetime.now(UTC) - timedelta(days=2)
        mock_get_cursors.return_value = {"ext_acc_1": stored}
        mock_start_run.return_value = MagicMock(id="run_cursor_3")
        default_since = datetime.now(UTC) - timedelta(days=90)

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                provider_type="mock_provider",
                since=default_since,
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        calls = mock_connector._rate_limited_fetch_transactions.await_args_list
        sices = {c.kwargs["account_id"]: c.args[0] for c in calls}
        assert sices == {
            "ext_acc_1": stored,  # resumes
            "ext_acc_2": default_since,  # new account: full default window
        }

    @patch("finance_sync.sync.orchestrator.get_connector_cursors")
    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_explicit_since_overrides_stored_cursor(
        self,
        mock_complete_run,
        mock_start_run,
        mock_get_cursors,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """An operator backfill (explicit since) covers every account."""
        stored = datetime.now(UTC) - timedelta(days=2)
        mock_get_cursors.return_value = {"ext_acc_1": stored}
        mock_start_run.return_value = MagicMock(id="run_cursor_4")
        explicit = datetime.now(UTC) - timedelta(days=45)

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                provider_type="mock_provider",
                since=explicit,
                log=MagicMock(),
                resume=False,
            )

        assert result.status == SyncRunStatus.COMPLETED
        # Cursor lookup is skipped; the explicit window is used
        mock_get_cursors.assert_not_awaited()
        call = mock_connector._rate_limited_fetch_transactions.await_args
        assert call.args[0] == explicit

    @patch("finance_sync.sync.orchestrator.get_connector_cursors")
    @patch("finance_sync.sync.orchestrator.upsert_sync_cursor")
    @patch("finance_sync.sync.orchestrator.start_sync_run")
    async def test_cursor_not_advanced_on_failure(
        self,
        mock_start_run,
        mock_upsert_cursor,
        mock_get_cursors,
        orchestrator,
        mock_uow,
    ) -> None:
        """A failed run never advances the stored cursor."""
        from finance_sync.connectors.exceptions import PermanentError

        mock_get_cursors.return_value = {
            "ext_acc_1": datetime.now(UTC) - timedelta(days=2)
        }
        mock_start_run.return_value = MagicMock(id="run_cursor_fail")

        connector = MagicMock()
        connector.name = "mock_provider"
        connector.authenticate = AsyncMock(
            side_effect=PermanentError("Bad credentials")
        )

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_pipeline(
                session=mock_uow.session,
                connector=connector,
                provider_type="mock_provider",
                since=datetime.now(UTC) - timedelta(days=90),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.FAILED
        mock_upsert_cursor.assert_not_awaited()


# ── Upsert helpers (unit) ─────────────────────────────────────────


class TestUpsertAccount:
    """Test _upsert_account logic in isolation."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        return SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )

    async def test_upsert_creates_new_account(
        self, orchestrator, sample_account_data
    ) -> None:
        """New account is created and outbox message emitted."""
        uow = MagicMock()
        uow.session = AsyncMock()  # Use AsyncMock for session
        uow.accounts.get_by_external_id = AsyncMock(return_value=None)

        result = await orchestrator._upsert_account(uow, sample_account_data)

        assert result is not None
        assert result.provider_key == "mock_provider"
        assert result.name == "Test Checking"

    async def test_upsert_updates_existing(
        self, orchestrator, sample_account_data
    ) -> None:
        """Existing account is updated when fields change."""
        from finance_sync.models import Account
        from finance_sync.models.enums import AccountType

        existing = Account(
            tenant_id="tenant_1",
            provider_key="mock_provider",
            external_account_id="ext_acc_1",
            name="Old Name",
            account_type=AccountType.CHECKING,
            currency_code="EUR",
        )

        uow = MagicMock()
        uow.session = AsyncMock()  # Use AsyncMock for session
        uow.accounts.get_by_external_id = AsyncMock(return_value=existing)

        result = await orchestrator._upsert_account(uow, sample_account_data)

        assert result is not None
        assert result.name == "Test Checking"


class TestUpsertTransaction:
    """Test _upsert_transaction logic in isolation."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        return SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )

    async def test_upsert_creates_new_transaction(
        self, orchestrator, sample_transaction_data
    ) -> None:
        """New transaction is created and outbox message emitted."""
        uow = MagicMock()
        uow.session = AsyncMock()  # Use AsyncMock for session
        uow.transactions.get_by_external_id = AsyncMock(return_value=None)

        result = await orchestrator._upsert_transaction(
            uow, sample_transaction_data, "account_id_1"
        )

        assert result is not None
        assert result.amount == Decimal("-42.50")
        assert result.description == "Coffee"


# ── Upsert scheduled payments / card transactions ─────────────────


class TestUpsertScheduledPayment:
    """Test _upsert_scheduled_payment logic in isolation."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        return SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )

    @pytest.fixture
    def sample_schedule_data(self) -> CanonicalScheduledPaymentData:
        return CanonicalScheduledPaymentData(
            provider_key="bunq",
            external_schedule_id="ext_sched_1",
            external_account_id="ext_acc_1",
            amount=Decimal("-150.00"),
            currency_code="EUR",
            frequency="monthly",
            interval=1,
            next_execution_date=datetime(2025, 7, 1, 0, 0, tzinfo=UTC),
            execution_count=6,
            counterparty_name="Landlord B.V.",
            description="Monthly rent",
            status="active",
        )

    async def test_upsert_creates_new_schedule(
        self, orchestrator, sample_schedule_data
    ) -> None:
        """New schedule is created with normalised enums."""
        uow = MagicMock()
        uow.session = AsyncMock()
        uow.scheduled_payments.get_by_external_id = AsyncMock(return_value=None)

        result = await orchestrator._upsert_scheduled_payment(
            uow, sample_schedule_data, "acct_uuid_1"
        )

        assert result is not None
        assert result.amount == Decimal("-150.00")
        assert result.frequency == "monthly"
        assert result.status == "active"
        assert result.account_id == "acct_uuid_1"
        assert result.execution_count == 6

    async def test_upsert_updates_existing_schedule(
        self, orchestrator, sample_schedule_data
    ) -> None:
        """Existing schedule is updated in place (idempotent re-run)."""
        uow = MagicMock()
        uow.session = AsyncMock()

        existing = MagicMock()
        existing.amount = Decimal("-100.00")  # stale amount
        existing.execution_count = 3
        existing.frequency = "weekly"
        existing.status = "paused"
        existing.next_execution_date = None
        existing.end_date = None
        existing.max_executions = None
        existing.counterparty_name = None
        existing.counterparty_iban = None
        existing.description = None
        existing.currency_code = "EUR"
        existing.interval = 1
        uow.scheduled_payments.get_by_external_id = AsyncMock(
            return_value=existing
        )

        result = await orchestrator._upsert_scheduled_payment(
            uow, sample_schedule_data, "acct_uuid_1"
        )

        assert result is existing
        assert existing.amount == Decimal("-150.00")
        assert existing.execution_count == 6
        assert existing.frequency == "monthly"
        assert existing.status == "active"


class TestUpsertCardTransaction:
    """Test _upsert_card_transaction logic in isolation."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        return SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )

    @pytest.fixture
    def sample_card_data(self) -> CanonicalCardTransactionData:
        return CanonicalCardTransactionData(
            provider_key="bunq",
            external_card_transaction_id="ext_card_1",
            external_account_id="7000001",  # card id, not an account id
            amount=Decimal("-42.50"),
            currency_code="EUR",
            merchant_name="Supermarket B.V.",
            merchant_city="Amsterdam",
            mcc="5411",
            card_id="7000001",
            card_type="DEBIT_CARD",
            occurred_at=datetime(2025, 6, 20, 14, 30, tzinfo=UTC),
            authorization_type="authorization",
            status="pending",
        )

    async def test_upsert_creates_new_card_txn_without_account(
        self, orchestrator, sample_card_data
    ) -> None:
        """Card id does not resolve to an account → account_id is None."""
        uow = MagicMock()
        uow.session = AsyncMock()
        uow.card_transactions.get_by_external_id = AsyncMock(return_value=None)
        # external_account_id is a card id — no account matches
        uow.accounts.get_by_external_id = AsyncMock(return_value=None)

        result = await orchestrator._upsert_card_transaction(
            uow, sample_card_data
        )

        assert result is not None
        assert result.amount == Decimal("-42.50")
        assert result.merchant_name == "Supermarket B.V."
        assert result.account_id is None
        assert result.authorization_type == "authorization"
        assert result.status == "pending"
        assert result.transaction_type == "card_payment"

    async def test_upsert_resolves_account_when_available(
        self, orchestrator, sample_card_data
    ) -> None:
        """When external_account_id matches an account, link it."""
        uow = MagicMock()
        uow.session = AsyncMock()
        uow.card_transactions.get_by_external_id = AsyncMock(return_value=None)
        acct = MagicMock()
        acct.id = "acct_uuid_1"
        uow.accounts.get_by_external_id = AsyncMock(return_value=acct)

        result = await orchestrator._upsert_card_transaction(
            uow, sample_card_data
        )

        assert result.account_id == "acct_uuid_1"
        uow.accounts.get_by_external_id.assert_awaited_once()

    async def test_upsert_updates_existing_card_txn(
        self, orchestrator, sample_card_data
    ) -> None:
        """Existing card txn is updated in place (idempotent re-run)."""
        uow = MagicMock()
        uow.session = AsyncMock()

        existing = MagicMock()
        existing.amount = Decimal("-10.00")
        existing.status = "booked"
        existing.merchant_name = None
        existing.merchant_city = None
        existing.merchant_country = None
        existing.mcc = None
        existing.card_id = None
        existing.card_type = None
        existing.card_last_four = None
        existing.occurred_at = None
        existing.booked_at = None
        existing.authorization_type = "other"
        existing.description = None
        existing.currency_code = "EUR"
        uow.card_transactions.get_by_external_id = AsyncMock(
            return_value=existing
        )

        result = await orchestrator._upsert_card_transaction(
            uow, sample_card_data
        )

        assert result is existing
        assert existing.amount == Decimal("-42.50")
        assert existing.status == "pending"
        assert existing.merchant_name == "Supermarket B.V."
        assert existing.authorization_type == "authorization"


# ── Bunq cards / scheduled payments pipeline ───────────────────────


class TestBunqCardsSync:
    """Test the run_bunq_cards_sync pipeline with a mocked UoW."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        session_factory = AsyncMock()
        registry = MagicMock()
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_1",
        )

    @pytest.fixture
    def sample_schedule_data(self) -> CanonicalScheduledPaymentData:
        return CanonicalScheduledPaymentData(
            provider_key="bunq",
            external_schedule_id="ext_sched_1",
            external_account_id="ext_acc_1",
            amount=Decimal("-150.00"),
            currency_code="EUR",
            frequency="monthly",
            interval=1,
            next_execution_date=datetime(2025, 7, 1, 0, 0, tzinfo=UTC),
            execution_count=6,
            counterparty_name="Landlord B.V.",
            description="Monthly rent",
            status="active",
        )

    @pytest.fixture
    def sample_card_data(self) -> CanonicalCardTransactionData:
        return CanonicalCardTransactionData(
            provider_key="bunq",
            external_card_transaction_id="ext_card_1",
            external_account_id="7000001",  # card id, not an account id
            amount=Decimal("-42.50"),
            currency_code="EUR",
            merchant_name="Supermarket B.V.",
            merchant_city="Amsterdam",
            mcc="5411",
            card_id="7000001",
            card_type="DEBIT_CARD",
            occurred_at=datetime(2025, 6, 20, 14, 30, tzinfo=UTC),
            authorization_type="authorization",
            status="pending",
        )

    @pytest.fixture
    def mock_connector(
        self, sample_account_data, sample_schedule_data, sample_card_data
    ):
        """Connector returning accounts, schedules and card txns."""
        connector = MagicMock()
        connector.name = "bunq"
        connector.authenticate = AsyncMock()
        connector._rate_limited_fetch_accounts = AsyncMock(
            return_value=[sample_account_data]
        )
        connector.transform_accounts = MagicMock(
            return_value=[sample_account_data]
        )
        connector.fetch_scheduled_payments = AsyncMock(
            return_value=[sample_schedule_data]
        )
        connector.transform_scheduled_payments = MagicMock(
            return_value=[sample_schedule_data]
        )
        connector.fetch_card_transactions = AsyncMock(
            return_value=[sample_card_data]
        )
        connector.transform_card_transactions = MagicMock(
            return_value=[sample_card_data]
        )
        return connector

    @pytest.fixture
    def mock_uow(self):
        """UnitOfWork with mocked repositories for the cards pipeline."""
        uow = MagicMock()
        session = AsyncMock()
        uow.session = session

        existing_account = MagicMock()
        existing_account.id = "acct_uuid_1"

        accounts_repo = AsyncMock()
        accounts_repo.get_by_external_id = AsyncMock(
            return_value=existing_account
        )
        uow.accounts = accounts_repo

        schedules_repo = AsyncMock()
        schedules_repo.get_by_external_id = AsyncMock(return_value=None)
        uow.scheduled_payments = schedules_repo

        card_txns_repo = AsyncMock()
        card_txns_repo.get_by_external_id = AsyncMock(return_value=None)
        uow.card_transactions = card_txns_repo

        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=None)
        uow.commit = AsyncMock()
        uow.rollback = AsyncMock()

        return uow

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_cards_pipeline_ingests(
        self,
        mock_complete_run,
        mock_start_run,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """Schedules + card txns are upserted and the run completes."""
        mock_run = MagicMock(id="run_cards_1")
        mock_start_run.return_value = mock_run
        mock_complete_run.return_value = mock_run

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_cards_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.schedules_synced == 1
        assert result.card_transactions_synced == 1
        assert result.error_message is None

        mock_connector.authenticate.assert_awaited_once()
        mock_connector.fetch_scheduled_payments.assert_awaited_once()
        mock_connector.fetch_card_transactions.assert_awaited_once()
        mock_complete_run.assert_awaited_once()
        # Items processed = schedules + card txns
        processed = mock_complete_run.call_args.kwargs.get("items_processed")
        assert processed == 2

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_cards_pipeline_idempotent_rerun(
        self,
        mock_complete_run,
        mock_start_run,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """A re-run finds existing records and does not insert duplicates."""
        mock_run = MagicMock(id="run_cards_2")
        mock_start_run.return_value = mock_run

        # Second run: both records already exist
        existing_schedule = MagicMock()
        existing_schedule.amount = Decimal("-150.00")
        existing_schedule.execution_count = 6
        existing_schedule.frequency = "monthly"
        existing_schedule.status = "active"
        existing_schedule.next_execution_date = None
        existing_schedule.end_date = None
        existing_schedule.max_executions = None
        existing_schedule.counterparty_name = None
        existing_schedule.counterparty_iban = None
        existing_schedule.description = None
        existing_schedule.currency_code = "EUR"
        existing_schedule.interval = 1

        existing_card = MagicMock()
        existing_card.amount = Decimal("-42.50")
        existing_card.status = "pending"
        existing_card.merchant_name = "Supermarket B.V."
        existing_card.merchant_city = "Amsterdam"
        existing_card.merchant_country = None
        existing_card.mcc = "5411"
        existing_card.card_id = "7000001"
        existing_card.card_type = "DEBIT_CARD"
        existing_card.card_last_four = None
        existing_card.occurred_at = datetime(2025, 6, 20, 14, 30, tzinfo=UTC)
        existing_card.booked_at = None
        existing_card.authorization_type = "authorization"
        existing_card.description = None
        existing_card.currency_code = "EUR"

        mock_uow.scheduled_payments.get_by_external_id = AsyncMock(
            return_value=existing_schedule
        )
        mock_uow.card_transactions.get_by_external_id = AsyncMock(
            return_value=existing_card
        )

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_cards_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.schedules_synced == 1
        assert result.card_transactions_synced == 1
        # No new entity inserts — both upserts hit the existing-record
        # branch (only the account upsert may emit an outbox message).
        from finance_sync.models import CardTransaction, ScheduledPayment

        added_entities = [
            c.args[0] for c in mock_uow.session.add.call_args_list
        ]
        assert not any(
            isinstance(e, (ScheduledPayment, CardTransaction))
            for e in added_entities
        )

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.upsert_sync_cursor")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_cards_pipeline_persists_cursor_on_success(
        self,
        mock_complete_run,
        mock_upsert_cursor,
        mock_start_run,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """A successful cards run advances the card_transactions cursor."""
        from finance_sync.sync.sync_cursor import RESOURCE_CARD_TRANSACTIONS

        mock_start_run.return_value = MagicMock(id="run_cards_cursor")

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_cards_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        upsert_kwargs = mock_upsert_cursor.await_args.kwargs
        assert upsert_kwargs["resource"] == RESOURCE_CARD_TRANSACTIONS
        assert upsert_kwargs["connector"] == "bunq_cards"
        assert upsert_kwargs["cursor"] is not None
        # Run record exposes the watermark
        complete_kwargs = mock_complete_run.await_args.kwargs
        assert complete_kwargs["cursor"] is not None

    @patch("finance_sync.sync.orchestrator.get_cursor")
    @patch(
        "finance_sync.sync.orchestrator.SyncOrchestrator._record_sync_metrics"
    )
    @patch(
        "finance_sync.sync.orchestrator.SyncOrchestrator._run_cards_pipeline"
    )
    async def test_cards_sync_resumes_from_stored_cursor(
        self,
        mock_pipeline,
        mock_record_metrics,
        mock_get_cursor,
    ) -> None:
        """run_bunq_cards_sync resumes from the stored cards cursor."""
        from finance_sync.sync.orchestrator import BunqCardsSyncResult

        # MagicMock factory: calling it yields an async-context-manager
        # session (AsyncMock factories return bare coroutines instead).
        orchestrator = SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )

        stored = datetime.now(UTC) - timedelta(days=2)
        mock_get_cursor.return_value = stored
        mock_pipeline.return_value = BunqCardsSyncResult(
            status=SyncRunStatus.COMPLETED,
            schedules_synced=0,
            card_transactions_synced=0,
            error_message=None,
            duration_s=0.0,
        )

        result = await orchestrator.run_bunq_cards_sync(MagicMock())

        assert result.status == SyncRunStatus.COMPLETED
        # Pipeline received the stored cursor as its since window
        assert mock_pipeline.await_args.args[2] == stored

    @patch("finance_sync.sync.orchestrator.get_cursor")
    @patch(
        "finance_sync.sync.orchestrator.SyncOrchestrator._record_sync_metrics"
    )
    @patch(
        "finance_sync.sync.orchestrator.SyncOrchestrator._run_cards_pipeline"
    )
    async def test_cards_sync_first_run_uses_default_window(
        self,
        mock_pipeline,
        mock_record_metrics,
        mock_get_cursor,
    ) -> None:
        """No stored cards cursor → 90-day default window."""
        from finance_sync.sync.orchestrator import BunqCardsSyncResult

        orchestrator = SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )

        mock_get_cursor.return_value = None
        mock_pipeline.return_value = BunqCardsSyncResult(
            status=SyncRunStatus.COMPLETED,
            schedules_synced=0,
            card_transactions_synced=0,
            error_message=None,
            duration_s=0.0,
        )

        result = await orchestrator.run_bunq_cards_sync(MagicMock())

        assert result.status == SyncRunStatus.COMPLETED
        since_arg = mock_pipeline.await_args.args[2]
        floor = datetime.now(UTC) - timedelta(days=90, minutes=1)
        assert since_arg >= floor
        assert since_arg <= datetime.now(UTC)

    @patch("finance_sync.sync.orchestrator.get_cursor")
    @patch(
        "finance_sync.sync.orchestrator.SyncOrchestrator._record_sync_metrics"
    )
    @patch(
        "finance_sync.sync.orchestrator.SyncOrchestrator._run_cards_pipeline"
    )
    async def test_cards_explicit_since_skips_cursor_lookup(
        self,
        mock_pipeline,
        mock_record_metrics,
        mock_get_cursor,
    ) -> None:
        """An explicit cards since (backfill) wins over the cursor."""
        from finance_sync.sync.orchestrator import BunqCardsSyncResult

        orchestrator = SyncOrchestrator(
            session_factory=MagicMock(),
            registry=MagicMock(),
            tenant_id="tenant_1",
        )

        explicit = datetime.now(UTC) - timedelta(days=400)
        mock_pipeline.return_value = BunqCardsSyncResult(
            status=SyncRunStatus.COMPLETED,
            schedules_synced=0,
            card_transactions_synced=0,
            error_message=None,
            duration_s=0.0,
        )

        result = await orchestrator.run_bunq_cards_sync(
            MagicMock(), since=explicit
        )

        assert result.status == SyncRunStatus.COMPLETED
        mock_get_cursor.assert_not_awaited()
        assert mock_pipeline.await_args.args[2] == explicit

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    async def test_cards_pipeline_permanent_error(
        self,
        mock_start_run,
        orchestrator,
        mock_uow,
    ) -> None:
        """PermanentError during cards sync marks the run failed."""
        mock_run = MagicMock(id="run_cards_3")
        mock_start_run.return_value = mock_run

        connector = MagicMock()
        connector.authenticate = AsyncMock(
            side_effect=PermanentError("Bad credentials")
        )
        connector.name = "bunq"

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_cards_pipeline(
                session=mock_uow.session,
                connector=connector,
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.FAILED
        assert "Bad credentials" in (result.error_message or "")

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_cards_pipeline_skips_schedule_without_account(
        self,
        mock_complete_run,
        mock_start_run,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """Schedule whose account is unknown is skipped, run still green."""
        mock_run = MagicMock(id="run_cards_4")
        mock_start_run.return_value = mock_run
        mock_complete_run.return_value = mock_run

        # No account matches the schedule's external account id
        mock_uow.accounts.get_by_external_id = AsyncMock(return_value=None)

        with patch("finance_sync.db.uow.UnitOfWork", return_value=mock_uow):
            result = await orchestrator._run_cards_pipeline(
                session=mock_uow.session,
                connector=mock_connector,
                since=datetime.now(UTC) - timedelta(days=30),
                log=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        # Schedule skipped (no account FK target); card txn still ingested
        mock_uow.scheduled_payments.get_by_external_id.assert_not_awaited()
        assert result.schedules_synced == 1  # fetched, but not persisted
        assert result.card_transactions_synced == 1


# ── Auto-reconciliation after sync ────────────────────────────────


class TestAutoReconciliationAfterSync:
    """Test that reconciliation runs automatically after a successful sync."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        session_factory = MagicMock()
        mock_session = AsyncMock()
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
        registry = MagicMock()
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_test_1",
        )

    async def test_reconciliation_runs_after_successful_sync(
        self, orchestrator
    ) -> None:
        """Successful sync triggers automatic reconciliation."""
        mock_connector = MagicMock()
        orchestrator._registry.get_connector = MagicMock(
            return_value=mock_connector
        )

        mock_result = SyncResult(
            status=SyncRunStatus.COMPLETED,
            accounts_synced=2,
            transactions_synced=10,
            error_message=None,
            duration_s=1.5,
        )

        def patch_run_pipeline(
            session, connector, provider_type, since, log, *, resume=True
        ):
            return mock_result

        with (
            patch.object(
                orchestrator,
                "_run_pipeline",
                side_effect=patch_run_pipeline,
            ),
            patch.object(
                orchestrator,
                "run_reconciliation",
                new=AsyncMock(
                    return_value=ReconciliationRunSummary(
                        run_id="rec_auto_1",
                        status=ReconciliationRunStatus.COMPLETED,
                        finding_count=3,
                    )
                ),
            ) as mock_rec,
        ):
            config = MagicMock()
            result = await orchestrator.run_sync(
                provider_type="mock_provider",
                config=config,
            )

        assert result.status == SyncRunStatus.COMPLETED
        mock_rec.assert_awaited_once()
        assert mock_rec.call_args.kwargs["date_from"] is not None

    async def test_reconciliation_skipped_on_failed_sync(
        self, orchestrator
    ) -> None:
        """Failed sync does NOT trigger automatic reconciliation."""
        mock_connector = MagicMock()
        orchestrator._registry.get_connector = MagicMock(
            return_value=mock_connector
        )

        mock_result = SyncResult(
            status=SyncRunStatus.FAILED,
            accounts_synced=0,
            transactions_synced=0,
            error_message="Sync failed",
            duration_s=0.5,
        )

        def patch_run_pipeline(
            session, connector, provider_type, since, log, *, resume=True
        ):
            return mock_result

        with (
            patch.object(
                orchestrator,
                "_run_pipeline",
                side_effect=patch_run_pipeline,
            ),
            patch.object(
                orchestrator,
                "run_reconciliation",
                new=AsyncMock(),
            ) as mock_rec,
        ):
            config = MagicMock()
            result = await orchestrator.run_sync(
                provider_type="mock_provider",
                config=config,
            )

        assert result.status == SyncRunStatus.FAILED
        mock_rec.assert_not_awaited()

    async def test_reconciliation_error_does_not_crash_sync(
        self, orchestrator
    ) -> None:
        """Sync result is returned even when reconciliation raises."""
        mock_connector = MagicMock()
        orchestrator._registry.get_connector = MagicMock(
            return_value=mock_connector
        )

        mock_result = SyncResult(
            status=SyncRunStatus.COMPLETED,
            accounts_synced=2,
            transactions_synced=10,
            error_message=None,
            duration_s=1.5,
        )

        def patch_run_pipeline(
            session, connector, provider_type, since, log, *, resume=True
        ):
            return mock_result

        with (
            patch.object(
                orchestrator,
                "_run_pipeline",
                side_effect=patch_run_pipeline,
            ),
            patch.object(
                orchestrator,
                "run_reconciliation",
                new=AsyncMock(
                    side_effect=RuntimeError("Reconciliation blew up")
                ),
            ) as mock_rec,
        ):
            config = MagicMock()
            result = await orchestrator.run_sync(
                provider_type="mock_provider",
                config=config,
            )

        # Sync result should still be returned as COMPLETED even though
        # reconciliation failed — the sync itself was successful.
        assert result.status == SyncRunStatus.COMPLETED
        mock_rec.assert_awaited_once()


class TestAutoReconciliationDisabled:
    """Test that auto-reconciliation can be disabled via settings."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        session_factory = MagicMock()
        mock_session = AsyncMock()
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
        registry = MagicMock()
        settings = MagicMock()
        settings.worker_job_reconciliation_after_sync_enabled = False
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_test_1",
            settings=settings,
        )

    async def test_reconciliation_skipped_when_disabled(
        self, orchestrator
    ) -> None:
        """When config flag is False, reconciliation is skipped after sync."""
        mock_connector = MagicMock()
        orchestrator._registry.get_connector = MagicMock(
            return_value=mock_connector
        )

        mock_result = SyncResult(
            status=SyncRunStatus.COMPLETED,
            accounts_synced=2,
            transactions_synced=10,
            error_message=None,
            duration_s=1.5,
        )

        def patch_run_pipeline(
            session, connector, provider_type, since, log, *, resume=True
        ):
            return mock_result

        with (
            patch.object(
                orchestrator,
                "_run_pipeline",
                side_effect=patch_run_pipeline,
            ),
            patch.object(
                orchestrator,
                "run_reconciliation",
                new=AsyncMock(),
            ) as mock_rec,
        ):
            config = MagicMock()
            result = await orchestrator.run_sync(
                provider_type="mock_provider",
                config=config,
            )

        assert result.status == SyncRunStatus.COMPLETED
        # Reconciliation should NOT have been called
        mock_rec.assert_not_awaited()


# ── SyncRun lifecycle (real SQLite) ───────────────────────────────


@pytest.fixture
async def sync_run_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SyncRunTestBase.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(SyncRunTestBase.metadata.drop_all)


@pytest.fixture
async def sync_run_factory(sync_run_engine):
    return async_sessionmaker(bind=sync_run_engine, expire_on_commit=False)


async def test_sync_run_create_and_complete(sync_run_factory) -> None:
    """Test creating and completing a SyncRun via SQLAlchemy."""
    run = SyncRunTestModel(connector="test_provider", status="running")

    async with sync_run_factory() as s:
        s.add(run)
        await s.commit()
        run_id = run.id

    # Complete it
    async with sync_run_factory() as s:
        from sqlalchemy import select

        result = await s.execute(
            select(SyncRunTestModel).where(SyncRunTestModel.id == run_id)
        )
        loaded = result.scalar_one()
        loaded.status = SyncRunStatus.COMPLETED.value
        loaded.completed_at = datetime.now(UTC)
        loaded.items_processed = 10
        await s.commit()

    # Verify
    async with sync_run_factory() as s:
        from sqlalchemy import select

        result = await s.execute(
            select(SyncRunTestModel).where(SyncRunTestModel.id == run_id)
        )
        final = result.scalar_one()
        assert final.status == SyncRunStatus.COMPLETED.value
        assert final.items_processed == 10


async def test_sync_run_failed(sync_run_factory) -> None:
    """Test a failed SyncRun."""
    run = SyncRunTestModel(connector="test_provider", status="running")

    async with sync_run_factory() as s:
        s.add(run)
        await s.commit()
        run_id = run.id

    async with sync_run_factory() as s:
        from sqlalchemy import select

        result = await s.execute(
            select(SyncRunTestModel).where(SyncRunTestModel.id == run_id)
        )
        loaded = result.scalar_one()
        loaded.status = SyncRunStatus.FAILED.value
        loaded.completed_at = datetime.now(UTC)
        loaded.items_processed = 0
        loaded.error_message = "Something broke"
        await s.commit()

    async with sync_run_factory() as s:
        from sqlalchemy import select

        result = await s.execute(
            select(SyncRunTestModel).where(SyncRunTestModel.id == run_id)
        )
        final = result.scalar_one()
        assert final.status == SyncRunStatus.FAILED.value
        assert final.items_processed == 0
        assert final.error_message == "Something broke"


# ── Full run_sync integration tests ─────────────────────────────────


class TestSyncOrchestratorRunSync:
    """Test the full run_sync flow with auto-reconciliation."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        session_factory = MagicMock()  # MagicMock, not AsyncMock — calling it
        # returns an object with __aenter__/__aexit__, not a coroutine
        registry = MagicMock()
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_1",
        )

    @pytest.fixture
    def mock_connector(self, sample_account_data, sample_transaction_data):
        """Mock connector that returns one account and one transaction."""
        connector = MagicMock()
        connector.name = "mock_provider"
        connector.authenticate = AsyncMock()
        connector._rate_limited_fetch_accounts = AsyncMock(
            return_value=[sample_account_data]
        )
        connector.transform_accounts = MagicMock(
            return_value=[sample_account_data]
        )
        connector.transform_transactions = MagicMock(
            return_value=[sample_transaction_data]
        )
        connector._rate_limited_fetch_transactions = AsyncMock(
            return_value=[sample_transaction_data]
        )
        return connector

    @pytest.fixture
    def mock_uow(self):
        """UnitOfWork with mocked repositories — returns None then account."""
        uow = MagicMock()
        uow.session = AsyncMock()
        existing_account = MagicMock()
        existing_account.id = "acct_uuid_1"
        accounts_repo = AsyncMock()
        accounts_repo.get_by_external_id = AsyncMock(
            side_effect=[None, existing_account]
        )
        uow.accounts = accounts_repo
        txn_repo = AsyncMock()
        txn_repo.get_by_external_id = AsyncMock(return_value=None)
        uow.transactions = txn_repo
        sync_runs_repo = AsyncMock()
        uow.sync_runs = sync_runs_repo
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=None)
        uow.commit = AsyncMock()
        uow.rollback = AsyncMock()
        return uow

    async def test_auto_reconciles_after_successful_sync(
        self, orchestrator, mock_connector, mock_uow
    ) -> None:
        """run_sync runs reconciliation automatically after a successful sync."""
        from unittest.mock import MagicMock, patch

        orchestrator._registry.get_connector = MagicMock(
            return_value=mock_connector
        )

        with (
            patch(
                "finance_sync.db.uow.UnitOfWork",
                return_value=mock_uow,
            ),
            patch(
                "finance_sync.sync.orchestrator.start_sync_run",
                return_value=MagicMock(id="sync_run_1"),
            ),
            patch(
                "finance_sync.sync.orchestrator.complete_sync_run",
            ),
        ):
            result = await orchestrator.run_sync(
                provider_type="mock_provider",
                config=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1
        assert result.transactions_synced >= 1
        assert result.error_message is None

    async def test_skips_reconciliation_on_failed_sync(
        self, orchestrator
    ) -> None:
        """When the sync pipeline fails, reconciliation is NOT called."""
        from unittest.mock import AsyncMock, MagicMock

        failing_connector = MagicMock()
        failing_connector.authenticate = AsyncMock(
            side_effect=PermanentError("Auth failed")
        )
        failing_connector.name = "mock_provider"
        orchestrator._registry.get_connector = MagicMock(
            return_value=failing_connector
        )

        mock_session = AsyncMock()
        orchestrator._session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        orchestrator._session_factory.return_value.__aexit__ = AsyncMock(
            return_value=None
        )

        # If reconciliation is called, the test should fail
        orchestrator.run_reconciliation = AsyncMock(  # type: ignore[assignment]
            side_effect=AssertionError("Should not be called")
        )

        result = await orchestrator.run_sync(
            provider_type="mock_provider",
            config=MagicMock(),
        )

        assert result.status == SyncRunStatus.FAILED

    async def test_reconciliation_failure_does_not_affect_sync_result(
        self, orchestrator, mock_connector, mock_uow
    ) -> None:
        """A reconciliation failure is logged but does not propagate."""
        from unittest.mock import AsyncMock, MagicMock, patch

        orchestrator._registry.get_connector = MagicMock(
            return_value=mock_connector
        )

        with (
            patch(
                "finance_sync.db.uow.UnitOfWork",
                return_value=mock_uow,
            ),
            patch(
                "finance_sync.sync.orchestrator.start_sync_run",
                return_value=MagicMock(id="sync_run_2"),
            ),
            patch(
                "finance_sync.sync.orchestrator.complete_sync_run",
            ),
            patch.object(
                orchestrator,
                "run_reconciliation",
                new=AsyncMock(
                    side_effect=RuntimeError("Reconciliation DB error")
                ),
            ),
        ):
            result = await orchestrator.run_sync(
                provider_type="mock_provider",
                config=MagicMock(),
            )

        # Sync result must remain COMPLETED even when reconciliation fails
        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1


class TestSyncOrchestratorRunSyncDisabled:
    """Test run_sync with auto-reconciliation disabled."""

    @pytest.fixture
    def orchestrator(self) -> SyncOrchestrator:
        session_factory = MagicMock()
        registry = MagicMock()
        settings = MagicMock()
        settings.worker_job_reconciliation_after_sync_enabled = False
        return SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id="tenant_1",
            settings=settings,
        )

    @pytest.fixture
    def mock_connector(self, sample_account_data, sample_transaction_data):
        """Mock connector that returns one account and one transaction."""
        connector = MagicMock()
        connector.name = "mock_provider"
        connector.authenticate = AsyncMock()
        connector._rate_limited_fetch_accounts = AsyncMock(
            return_value=[sample_account_data]
        )
        connector.transform_accounts = MagicMock(
            return_value=[sample_account_data]
        )
        connector.transform_transactions = MagicMock(
            return_value=[sample_transaction_data]
        )
        connector._rate_limited_fetch_transactions = AsyncMock(
            return_value=[sample_transaction_data]
        )
        return connector

    @pytest.fixture
    def mock_uow(self):
        """UnitOfWork with mocked repositories."""
        uow = MagicMock()
        uow.session = AsyncMock()
        existing_account = MagicMock()
        existing_account.id = "acct_uuid_1"
        accounts_repo = AsyncMock()
        accounts_repo.get_by_external_id = AsyncMock(
            side_effect=[None, existing_account]
        )
        uow.accounts = accounts_repo
        txn_repo = AsyncMock()
        txn_repo.get_by_external_id = AsyncMock(return_value=None)
        uow.transactions = txn_repo
        sync_runs_repo = AsyncMock()
        uow.sync_runs = sync_runs_repo
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=None)
        uow.commit = AsyncMock()
        uow.rollback = AsyncMock()
        return uow

    @patch("finance_sync.sync.orchestrator.start_sync_run")
    @patch("finance_sync.sync.orchestrator.complete_sync_run")
    async def test_skips_reconciliation_when_disabled(
        self,
        mock_complete_run,
        mock_start_run,
        orchestrator,
        mock_connector,
        mock_uow,
    ) -> None:
        """When auto-reconciliation is disabled, run_sync does NOT call it."""
        from unittest.mock import AsyncMock, MagicMock, patch

        orchestrator._registry.get_connector = MagicMock(
            return_value=mock_connector
        )

        # If reconciliation is called, the test should fail
        orchestrator.run_reconciliation = AsyncMock(  # type: ignore[assignment]
            side_effect=AssertionError("Should not be called")
        )

        with (
            patch(
                "finance_sync.db.uow.UnitOfWork",
                return_value=mock_uow,
            ),
        ):
            result = await orchestrator.run_sync(
                provider_type="mock_provider",
                config=MagicMock(),
            )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1


class TestRecordSyncMetrics:
    """Prometheus metric recording on sync completion (G-06)."""

    def test_record_completed_run(self) -> None:
        """Completed run increments the counter and sets the duration."""
        from prometheus_client import REGISTRY

        from finance_sync.sync.orchestrator import (
            SyncOrchestrator,
            SyncResult,
        )

        result = SyncResult(
            status=SyncRunStatus.COMPLETED,
            accounts_synced=2,
            transactions_synced=7,
            error_message=None,
            duration_s=12.5,
        )
        SyncOrchestrator._record_sync_metrics("bunq", result)

        assert (
            REGISTRY.get_sample_value(
                "sync_runs_total",
                {"provider": "bunq", "status": "completed"},
            )
            == 1.0
        )
        assert (
            REGISTRY.get_sample_value(
                "sync_run_duration_seconds",
                {"provider": "bunq"},
            )
            == 12.5
        )
        assert (
            REGISTRY.get_sample_value(
                "transactions_ingested_total",
                {"provider": "bunq"},
            )
            == 7.0
        )

    def test_record_failed_run(self) -> None:
        """Failed run increments the failed-status counter."""
        from prometheus_client import REGISTRY

        from finance_sync.sync.orchestrator import (
            SyncOrchestrator,
            SyncResult,
        )

        result = SyncResult(
            status=SyncRunStatus.FAILED,
            accounts_synced=0,
            transactions_synced=0,
            error_message="boom",
            duration_s=3.0,
        )
        SyncOrchestrator._record_sync_metrics("bunq", result)

        assert (
            REGISTRY.get_sample_value(
                "sync_runs_total",
                {"provider": "bunq", "status": "failed"},
            )
            == 1.0
        )

    def test_record_cards_run_uses_card_transactions(self) -> None:
        """Cards pipeline result records card transaction count."""
        from prometheus_client import REGISTRY

        from finance_sync.sync.orchestrator import (
            BunqCardsSyncResult,
            SyncOrchestrator,
        )

        result = BunqCardsSyncResult(
            status=SyncRunStatus.COMPLETED,
            schedules_synced=1,
            card_transactions_synced=4,
            error_message=None,
            duration_s=2.0,
        )
        SyncOrchestrator._record_sync_metrics("bunq_cards", result)

        assert (
            REGISTRY.get_sample_value(
                "sync_runs_total",
                {"provider": "bunq_cards", "status": "completed"},
            )
            == 1.0
        )
        assert (
            REGISTRY.get_sample_value(
                "transactions_ingested_total",
                {"provider": "bunq_cards"},
            )
            == 4.0
        )
