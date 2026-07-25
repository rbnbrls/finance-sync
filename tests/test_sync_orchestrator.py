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

    async def test_run_reconciliation_emits_outbox(
        self, orchestrator
    ) -> None:
        """Outbox message is emitted on successful reconciliation."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = "rec_run_outbox"
        mock_run.status = ReconciliationRunStatus.COMPLETED
        mock_run.finding_count = 5
        mock_run.summary = {"by_kind": {"duplicate_transaction": 5}, "by_severity": {"warning": 5}}

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

        def patch_run_pipeline(session, connector, provider_type, since, log):
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

        def patch_run_pipeline(session, connector, provider_type, since, log):
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

        def patch_run_pipeline(session, connector, provider_type, since, log):
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
                new=AsyncMock(side_effect=RuntimeError("Reconciliation blew up")),
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

        def patch_run_pipeline(session, connector, provider_type, since, log):
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
        from unittest.mock import AsyncMock, MagicMock, patch

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
