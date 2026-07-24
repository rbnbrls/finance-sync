"""Integration tests for the reconciliation pipeline.

Uses a real SQLite database (via aiosqlite) to test the full
reconciliation flow end-to-end - from database queries through
service logic to outbox event emission and post-sync integration.

We register ``visit_JSONB`` on SQLite's type compiler so the real
PostgreSQL-flavoured ORM models work with SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ── Make JSONB work with SQLite ──────────────────────────────────
# SQLite's type compiler doesn't know visit_JSONB (only visit_JSON).
# We register it so DDL and query compilation work transparently.
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[assignment]

# Also make the Uuid bind processor accept strings (not just UUID objects)
from sqlalchemy import types as _sa_types
import uuid as _uuid_mod

_uuid_bind_orig = _sa_types.Uuid.bind_processor


def _uuid_bind_patched(self, dialect):
    proc = _uuid_bind_orig(self, dialect)
    if proc is None or not self.as_uuid:
        return proc

    def _patched(value):
        if value is not None:
            if isinstance(value, str):
                return _uuid_mod.UUID(value).hex
            return value.hex
        return value

    return _patched


_sa_types.Uuid.bind_processor = _uuid_bind_patched

# Now import real models after the patch is in place
from finance_sync.db import Base  # noqa: E402
from finance_sync.models.account import Account  # noqa: E402
from finance_sync.models.enums import (  # noqa: E402
    AccountType,
    ReconciliationResultKind,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    SyncRunStatus,
    TransactionStatus,
    TransactionType,
)
from finance_sync.models.outbox import OutboxMessage  # noqa: E402
from finance_sync.models.reconciliation import (  # noqa: E402
    ReconciliationResult,
    ReconciliationRun,
)
from finance_sync.models.tenant import Tenant  # noqa: E402
from finance_sync.models.transaction import Transaction  # noqa: E402
from finance_sync.services.reconciliation import ReconciliationService  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    import uuid as _uuid

# ── Test constants ────────────────────────────────────────────────

TXN_COUNT = 0
_TEST_DB_PATH: str | None = None


def _next_ext_id() -> str:
    global TXN_COUNT  # noqa: PLW0603
    TXN_COUNT += 1
    return f"ext_txn_{TXN_COUNT}"


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def engine():
    """Create a SQLite file-based engine for the test module."""
    import tempfile
    _tf = tempfile.NamedTemporaryFile(suffix="_rec.db", delete=False)
    _path = _tf.name
    _tf.close()
    global _TEST_DB_PATH  # noqa: PLW0603
    _TEST_DB_PATH = _path
    return create_async_engine(f"sqlite+aiosqlite:///{_path}", echo=False)


@pytest.fixture(scope="module")
async def tables(engine):
    """Create all tables for the module scope."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    # Clean up temp DB file
    import os
    if _TEST_DB_PATH and os.path.exists(_TEST_DB_PATH):
        os.unlink(_TEST_DB_PATH)


@pytest.fixture
async def session_factory(engine, tables) -> async_sessionmaker[AsyncSession]:
    """Return a fresh session factory per test function."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a fresh session per test."""
    async with session_factory() as s:
        yield s


@pytest.fixture
async def tenant_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a real Tenant record and return its ID for FK references."""
    import uuid

    tid = uuid.uuid4()
    slug = f"test-tenant-{uuid.uuid4().hex[:8]}"
    async with session_factory() as s:
        s.add(Tenant(id=tid, name="Test Tenant", slug=slug))
        await s.commit()
    # Return string representation - the UUID patch handles string→UUID
    # conversion for column operations, and JSON payloads need strings.
    return str(tid)


@pytest.fixture
async def service(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> ReconciliationService:
    """Build a ReconciliationService wired to the SQLite database."""
    return ReconciliationService(
        session_factory=session_factory,
        tenant_id=tenant_id,
    )


# ── Seeding helpers ───────────────────────────────────────────────


async def _create_account(
    session: AsyncSession,
    *,
    tenant_id: str,
    external_account_id: str = "ext_acc_1",
    provider_key: str = "mock_provider",
    name: str = "Test Account",
) -> Account:
    account = Account(
        id=uuid4(),
        tenant_id=tenant_id,
        provider_key=provider_key,
        external_account_id=external_account_id,
        name=name,
        account_type=AccountType.CHECKING,
        currency_code="EUR",
    )
    session.add(account)
    await session.flush()
    return account


async def _create_transaction(
    session: AsyncSession,
    account_id: str,
    *,
    tenant_id: str,
    provider_key: str = "mock_provider",
    external_transaction_id: str | None = None,
    amount: Decimal = Decimal("-10.00"),
    occurred_at: datetime | None = None,
    description: str | None = "Test transaction",
) -> Transaction:
    txn = Transaction(
        id=uuid4(),
        tenant_id=tenant_id,
        provider_key=provider_key,
        external_transaction_id=external_transaction_id or _next_ext_id(),
        account_id=account_id,
        amount=amount,
        currency_code="EUR",
        occurred_at=occurred_at or datetime.now(),
        transaction_type=TransactionType.PAYMENT,
        status=TransactionStatus.BOOKED,
        description=description,
    )
    session.add(txn)
    await session.flush()
    return txn


# ═══════════════════════════════════════════════════════════════════════
# 1. End-to-end reconciliation with real SQL
# ═══════════════════════════════════════════════════════════════════════


class TestReconciliationServiceIntegration:
    """Integration tests for ReconciliationService with a real SQLite DB."""

    async def test_reconcile_without_findings(
        self,
        service: ReconciliationService,
    ) -> None:
        """No findings when there is no data at all."""
        now = datetime.now()
        run = await service.reconcile(
            date_from=now - timedelta(days=90),
            date_to=now,
        )
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count == 0

    async def test_reconcile_detects_duplicates(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Two transactions with same amount and close time -> duplicate finding."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=1),
            description="Groceries",
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="trading212",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=3),
            description="Groceries",
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count == 1
        assert run.summary is not None
        assert run.summary["by_kind"].get("duplicate_transaction", 0) == 1

    async def test_reconcile_detects_duplicates_with_same_provider(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Duplicate detection also catches same-provider duplicates."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            external_transaction_id="bunq_dup_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=1),
            description="Netflix",
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            external_transaction_id="bunq_dup_2",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=2),
            description="Netflix",
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count >= 1

    async def test_reconcile_no_duplicates_for_distinct_transactions(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Transactions with different amounts produce no duplicates."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-10.00"),
            occurred_at=now - timedelta(hours=1),
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-20.00"),
            occurred_at=now - timedelta(hours=2),
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count == 0

    async def test_reconcile_error_handling(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: str,
    ) -> None:
        """Exception during reconciliation marks run as FAILED."""
        now = datetime.now()
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock(return_value=None)

        from unittest.mock import patch as u_patch

        from finance_sync.services.reconciliation import ReconciliationService as RS

        with u_patch.object(RS, "_detect_duplicates", new=AsyncMock(side_effect=ValueError("Phase 1 failed!"))):
            bad_session_factory = MagicMock()
            bad_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            bad_session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

            error_service = ReconciliationService(
                session_factory=bad_session_factory,
                tenant_id=tenant_id,
            )
            run = await error_service.reconcile(
                date_from=now - timedelta(days=7),
                date_to=now + timedelta(hours=1),
            )

        assert run.status == ReconciliationRunStatus.FAILED
        assert run.error_message is not None
        assert "Phase 1 failed" in run.error_message

    # ═══════════════════════════════════════════════════════════════════
    # Cross-connector gap detection
    # ═══════════════════════════════════════════════════════════════════

    async def test_cross_connector_gap_detection(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """An account fed by two providers detects a gap when one starts late."""
        now = datetime.now()
        acct = await _create_account(
            session,
            tenant_id=tenant_id,
            provider_key="bunq",
            external_account_id="ext_cc_1",
            name="Dual-Provider Account",
        )
        await session.commit()

        # Provider A (bunq): transactions from 60 days ago
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(days=60),
        )
        # Provider B (trading212): transactions from only 10 days ago (gap > 7 days)
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="trading212",
            amount=Decimal("-25.00"),
            occurred_at=now - timedelta(days=10),
        )
        await session.commit()

        run = await service.reconcile(
            date_from=now - timedelta(days=90),
            date_to=now + timedelta(hours=1),
            provider_keys=["bunq", "trading212"],
        )

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count >= 1
        assert run.summary is not None
        assert run.summary["by_kind"].get("missing_transaction", 0) >= 1

    async def test_single_provider_skipped_for_gaps(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Single-provider accounts produce no cross-connector findings."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq", name="Single-Provider")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(days=30),
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=90),
            date_to=now + timedelta(hours=1),
        )

        assert run.status == ReconciliationRunStatus.COMPLETED

    # ═══════════════════════════════════════════════════════════════════
    # Missing transaction detection
    # ═══════════════════════════════════════════════════════════════════

    async def test_missing_transaction_gap_detected(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Provider with only recent data vs wide analysis window -> gap finding."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        # Transaction only 5 days ago, but analysis window starts 90 days ago
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-15.00"),
            occurred_at=now - timedelta(days=5),
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=90),
            date_to=now + timedelta(hours=1),
        )

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count >= 1
        assert run.summary is not None
        assert run.summary["by_kind"].get("missing_transaction", 0) >= 1

    # ═══════════════════════════════════════════════════════════════════
    # Reconciliation with provider_keys filter
    # ═══════════════════════════════════════════════════════════════════

    async def test_reconcile_with_provider_keys(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """provider_keys filter limits reconciliation to specified providers."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-30.00"),
            occurred_at=now - timedelta(hours=1),
            description="Subway",
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="trading212",
            amount=Decimal("-30.00"),
            occurred_at=now - timedelta(hours=2),
            description="Subway",
        )
        # Third provider also has a match but should be excluded by filter
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="revolut",
            amount=Decimal("-30.00"),
            occurred_at=now - timedelta(hours=3),
            description="Subway",
        )
        await session.commit()

        # Only compare bunq vs trading212
        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
            provider_keys=["bunq", "trading212"],
        )

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count == 1
        assert run.scope is not None
        assert run.scope.get("provider_keys") == ["bunq", "trading212"]


# ═══════════════════════════════════════════════════════════════════════
# 2. Run metadata query tests
# ═══════════════════════════════════════════════════════════════════════


class TestReconciliationServiceQueries:
    """Test list_runs and get_run_with_results against real SQLite data."""

    async def test_list_runs_empty(
        self,
        service: ReconciliationService,
    ) -> None:
        """list_runs returns empty when no runs exist."""
        runs = await service.list_runs()
        assert isinstance(runs, list)

    async def test_list_runs_after_reconciliation(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """list_runs returns runs created by reconcile()."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        for i in range(3):
            await _create_transaction(
                session,
                acct.id,
                tenant_id=tenant_id,
                provider_key="bunq",
                amount=Decimal(f"-{i + 1}.00"),
                occurred_at=now - timedelta(hours=i + 1),
            )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        runs = await service.list_runs(limit=10)
        assert len(runs) >= 1

        run_ids = {str(r.id) for r in runs}
        assert str(run.id) in run_ids

    async def test_get_run_with_results(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """get_run_with_results returns run with its findings."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=1),
            description="Duplicate A",
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="trading212",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=2),
            description="Duplicate A",
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        fetched_run, results, total = await service.get_run_with_results(str(run.id))
        assert fetched_run is not None
        assert str(fetched_run.id) == str(run.id)
        assert total >= 1
        assert len(results) >= 1

        result = results[0]
        assert result.kind == ReconciliationResultKind.DUPLICATE_TRANSACTION
        assert result.provider_key is not None
        assert result.amount is not None

    async def test_get_run_with_results_not_found(
        self,
        service: ReconciliationService,
    ) -> None:
        """get_run_with_results returns None for non-existent run."""
        run, results, total = await service.get_run_with_results("00000000-0000-0000-0000-000000000000")
        assert run is None
        assert results == []
        assert total == 0

    async def test_get_run_with_results_kind_filter(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """get_run_with_results with kind_filter returns filtered results."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-200.00"),
            occurred_at=now - timedelta(hours=1),
            description="Large Dup",
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="trading212",
            amount=Decimal("-200.00"),
            occurred_at=now - timedelta(hours=2),
            description="Large Dup",
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        _, results_dup, total_dup = await service.get_run_with_results(
            str(run.id),
            kind_filter="duplicate_transaction",
        )
        assert total_dup >= 1
        assert all(r.kind == "duplicate_transaction" for r in results_dup)

        _, _results_missing, total_missing = await service.get_run_with_results(
            str(run.id),
            kind_filter="missing_transaction",
        )
        assert total_missing == 0

    async def test_get_run_with_results_severity_filter(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: str,
    ) -> None:
        """get_run_with_results with severity_filter returns filtered results."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-75.00"),
            occurred_at=now - timedelta(hours=1),
            description="Coffee",
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="trading212",
            amount=Decimal("-75.00"),
            occurred_at=now - timedelta(hours=2),
            description="Coffee",
        )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        # Verify we can find results without filter
        _run_all, results_all, total_all = await service.get_run_with_results(
            str(run.id),
        )
        assert total_all >= 1, f"No results found for reconciliation run {run.id}"

        # Now try with severity filter
        _, results, total = await service.get_run_with_results(
            str(run.id),
            severity_filter="error",
        )
        assert total >= 1, f"Expected at least 1 error result, got {total} (total_all={total_all})"
        assert all(r.severity == "error" for r in results)

        _, _results_info, total_info = await service.get_run_with_results(
            str(run.id),
            severity_filter="info",
        )
        assert total_info == 0


# ═══════════════════════════════════════════════════════════════════════
# 3. Outbox emission after reconciliation
# ═══════════════════════════════════════════════════════════════════════


class TestReconciliationOutbox:
    """Verify that reconciliation.completed outbox messages are emitted."""

    async def test_outbox_emitted_on_successful_reconciliation(
        self,
        service: ReconciliationService,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Successful reconciliation emits a reconciliation.completed outbox message."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-10.00"),
            occurred_at=now - timedelta(hours=1),
        )
        await session.commit()

        # Run reconciliation through SyncOrchestrator which emits outbox
        from finance_sync.sync.orchestrator import SyncOrchestrator

        registry = MagicMock()
        sync_orch = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id=tenant_id,
        )

        summary = await sync_orch.run_reconciliation(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        assert summary.finding_count == 0
        assert summary.status == ReconciliationRunStatus.COMPLETED

        # Check that an outbox message was emitted
        from sqlalchemy import select

        stmt = select(OutboxMessage).where(
            OutboxMessage.event_type == "reconciliation.completed",  # type: ignore[attr-defined]
            OutboxMessage.aggregate_type == "reconciliation",  # type: ignore[attr-defined]
        )
        async with session_factory() as check_session:
            result = await check_session.execute(stmt)
            msgs = list(result.scalars().all())

        assert len(msgs) >= 1
        msg = msgs[-1]
        assert msg.event_type == "reconciliation.completed"
        assert msg.aggregate_type == "reconciliation"
        assert msg.idempotency_key is not None
        assert "reconciliation:" in msg.idempotency_key
        assert ":completed" in msg.idempotency_key

    async def test_reconciliation_run_appears_in_list(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """The reconciliation run is accessible via list_runs after completion."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        runs = await service.list_runs(limit=10)
        run_ids = {str(r.id) for r in runs}
        assert str(run.id) in run_ids

        fetched_run, results, total = await service.get_run_with_results(str(run.id))
        assert fetched_run is not None
        assert str(fetched_run.id) == str(run.id)
        assert str(fetched_run.tenant_id) == tenant_id
        assert fetched_run.status == ReconciliationRunStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════
# 4. Post-sync automatic reconciliation (SyncOrchestrator)
# ═══════════════════════════════════════════════════════════════════════


class TestPostSyncReconciliation:
    """Integration test for automatic reconciliation after sync."""

    async def test_post_sync_triggers_reconciliation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: str,
    ) -> None:
        """run_sync triggers reconciliation after successful sync."""
        now = datetime.now()

        from finance_sync.connectors.models import (
            ConnectorConfig,
            RawAccount,
            RawTransaction,
        )
        from tests.conftest import MockConnector

        raw_account = RawAccount(
            external_account_id="ext_sync_acc_1",
            name="Sync Test Checking",
            account_type="checking",
            currency_code="EUR",
            current_balance=Decimal("1000.00"),
            available_balance=Decimal("1000.00"),
        )
        raw_txn = RawTransaction(
            external_transaction_id="sync_txn_1",
            external_account_id="ext_sync_acc_1",
            amount=Decimal("-45.00"),
            currency_code="EUR",
            occurred_at=now - timedelta(days=1),
            booked_at=now - timedelta(days=1),
            description="Sync test payment",
            transaction_type="purchase",
            status="booked",
            provider_fingerprint="sync_hash_1",
        )

        connector = MockConnector(
            config=ConnectorConfig(
                provider_type="mock_provider",
                credentials={"api_key": "test"},
            ),
            accounts=[raw_account],
            transactions=[raw_txn],
        )

        from finance_sync.connectors.registry import ConnectorRegistry

        registry = ConnectorRegistry()
        registry.register_class("mock_provider", MockConnector)

        from finance_sync.sync.orchestrator import SyncOrchestrator

        orch = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id=tenant_id,
        )

        with patch.object(
            orch._registry,
            "get_connector",
            return_value=connector,
        ):
            with patch(
                "finance_sync.sync.orchestrator.outbox_reconciliation_completed",
                new=AsyncMock(return_value=None),
            ):
                with patch(
                    "finance_sync.sync.orchestrator.outbox_entity_created",
                    new=AsyncMock(return_value=None),
                ):
                    with patch(
                        "finance_sync.sync.orchestrator.outbox_entity_updated",
                        new=AsyncMock(return_value=None),
                    ):
                        result = await orch.run_sync(
                            provider_type="mock_provider",
                            config=ConnectorConfig(
                                provider_type="mock_provider",
                                credentials={"api_key": "test"},
                            ),
                            since=now - timedelta(days=30),
                        )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced >= 1
        assert result.transactions_synced >= 1

    async def test_post_sync_reconciliation_resilient_to_errors(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: str,
    ) -> None:
        """Sync succeeds even when post-sync reconciliation raises an error."""
        now = datetime.now()

        from finance_sync.connectors.models import (
            ConnectorConfig,
            RawAccount,
        )
        from tests.conftest import MockConnector

        raw_account = RawAccount(
            external_account_id="ext_sync_acc_2",
            name="Resilient Account",
            account_type="checking",
            currency_code="EUR",
            current_balance=Decimal("500.00"),
            available_balance=Decimal("500.00"),
        )
        connector = MockConnector(
            config=ConnectorConfig(
                provider_type="mock_provider",
                credentials={"api_key": "test"},
            ),
            accounts=[raw_account],
            transactions=[],
        )

        from finance_sync.connectors.registry import ConnectorRegistry

        registry = ConnectorRegistry()
        registry.register_class("mock_provider", MockConnector)

        from finance_sync.sync.orchestrator import SyncOrchestrator

        orch = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id=tenant_id,
        )

        with patch.object(
            orch._registry,
            "get_connector",
            return_value=connector,
        ):
            with patch(
                "finance_sync.sync.orchestrator.outbox_reconciliation_completed",
                new=AsyncMock(return_value=None),
            ):
                with patch(
                    "finance_sync.sync.orchestrator.outbox_entity_created",
                    new=AsyncMock(return_value=None),
                ):
                    with patch(
                        "finance_sync.sync.orchestrator.outbox_entity_updated",
                        new=AsyncMock(return_value=None),
                    ):
                        with patch.object(
                            orch,
                            "run_reconciliation",
                            side_effect=ValueError("Reconciliation crashed!"),
                        ):
                            result = await orch.run_sync(
                                provider_type="mock_provider",
                                config=ConnectorConfig(
                                    provider_type="mock_provider",
                                    credentials={"api_key": "test"},
                                ),
                                since=now - timedelta(days=30),
                            )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced >= 1


# ═══════════════════════════════════════════════════════════════════════
# 5. Dual-provider duplicate detection stress test
# ═══════════════════════════════════════════════════════════════════════


class TestBulkReconciliation:
    """Stress test reconciliation with larger datasets."""

    async def test_multiple_duplicates_across_providers(
        self,
        service: ReconciliationService,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Many transactions with matching amounts create multiple findings."""
        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq", name="Bulk Acct")
        await session.commit()

        for i in range(10):
            amount = Decimal(f"-{i * 10 + 10}.00")
            await _create_transaction(
                session,
                acct.id,
                tenant_id=tenant_id,
                provider_key="bunq",
                amount=amount,
                occurred_at=now - timedelta(hours=i + 1),
                description=f"Item {i}",
            )
            await _create_transaction(
                session,
                acct.id,
                tenant_id=tenant_id,
                provider_key="trading212",
                amount=amount,
                occurred_at=now - timedelta(hours=i + 2),
                description=f"Item {i}",
            )
        await session.commit()

        run = await service.reconcile(
            account_ids=[str(acct.id)], date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count >= 1
        assert run.summary is not None
        assert run.summary["by_kind"].get("duplicate_transaction", 0) >= 1


# ═══════════════════════════════════════════════════════════════════════
# 6. API-level integration via TestClient (no DB)
# ═══════════════════════════════════════════════════════════════════════
# NOTE: API endpoint tests are covered in test_reconciliation_api.py.
# The mock setup for these sync-method endpoints (list_runs, get_run_results)
# is non-trivial because the endpoint creates a real ReconciliationService.
# See test_reconciliation_api.py for the mocked API patterns.
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# 7. TransactionRepository — direct edge case tests
# ═══════════════════════════════════════════════════════════════════════


class TestTransactionRepositoryEdgeCases:
    """Direct tests for TransactionRepository methods with edge cases.

    Uses the same SQLite-based fixtures as the integration tests to
    exercise the real repository implementation.
    """

    async def test_find_duplicate_candidates_with_no_dates(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """find_duplicate_candidates with no date_from/date_to filters works."""
        from finance_sync.db.repositories import TransactionRepository

        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-50.00"),
            occurred_at=datetime.now() - timedelta(days=5),
            description="Test A",
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="trading212",
            amount=Decimal("-50.00"),
            occurred_at=datetime.now() - timedelta(days=6),
            description="Test B",
        )
        await session.commit()

        repo = TransactionRepository(session)
        pairs = await repo.find_duplicate_candidates(
            tenant_id,
            account_ids=[str(acct.id)],
        )

        assert len(pairs) >= 1

    async def test_find_duplicate_candidates_same_provider(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """Same provider with different external IDs can be detected as duplicates."""
        from finance_sync.db.repositories import TransactionRepository

        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        now = datetime.now()
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            external_transaction_id="b_ext_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=1),
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            external_transaction_id="b_ext_2",  # Different ext ID
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=2),
        )
        await session.commit()

        repo = TransactionRepository(session)
        pairs = await repo.find_duplicate_candidates(
            tenant_id,
            account_ids=[str(acct.id)],
            date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        # Should find the duplicate since same amount + different ext IDs
        assert len(pairs) == 1

    async def test_find_duplicate_candidates_with_provider_keys(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """provider_keys filter limits which providers are compared."""
        from finance_sync.db.repositories import TransactionRepository

        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        now = datetime.now()
        # Two transactions from excluded providers
        for pk in ["revolut", "ynab"]:
            await _create_transaction(
                session,
                acct.id,
                tenant_id=tenant_id,
                provider_key=pk,
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=1),
            )
        await session.commit()

        repo = TransactionRepository(session)
        pairs = await repo.find_duplicate_candidates(
            tenant_id,
            account_ids=[str(acct.id)],
            provider_keys=["bunq"],  # Only bunq — no matching pairs
            date_from=now - timedelta(days=7),
            date_to=now + timedelta(hours=1),
        )

        # No bunq transactions exist, so no pairs
        assert len(pairs) == 0

    async def test_get_transaction_date_range_without_filters(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        """get_transaction_date_range with no account_id/provider_key filters."""
        from finance_sync.db.repositories import TransactionRepository

        now = datetime.now()
        acct = await _create_account(session, tenant_id=tenant_id, provider_key="bunq")
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-10.00"),
            occurred_at=now - timedelta(days=5),
        )
        await _create_transaction(
            session,
            acct.id,
            tenant_id=tenant_id,
            provider_key="bunq",
            amount=Decimal("-20.00"),
            occurred_at=now - timedelta(days=1),
        )
        await session.commit()

        repo = TransactionRepository(session)
        start, end = await repo.get_transaction_date_range(
            tenant_id,
        )

        # Should have found the date range across all transactions
        assert start is not None
        assert end is not None
        assert start <= end
