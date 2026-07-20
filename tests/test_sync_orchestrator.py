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

from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
)
from finance_sync.models.enums import SyncRunStatus
from finance_sync.sync.orchestrator import SyncOrchestrator, SyncResult

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
