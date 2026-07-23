"""Tests for the reconciliation service.

Uses mocked repositories to test the ReconciliationService logic
in isolation, avoiding the need for a PostgreSQL-compatible database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from finance_sync.models.enums import (
    ReconciliationResultKind,
    ReconciliationRunStatus,
)


# ═══════════════════════════════════════════════════════════════════════
# Enums & model basics
# ═══════════════════════════════════════════════════════════════════════


class TestReconciliationEnums:
    """Verify enum values used by reconciliation."""

    def test_kind_values(self) -> None:
        assert ReconciliationResultKind.DUPLICATE_TRANSACTION == "duplicate_transaction"
        assert ReconciliationResultKind.MISSING_TRANSACTION == "missing_transaction"
        assert ReconciliationResultKind.CROSS_CONNECTOR_MISMATCH == "cross_connector_mismatch"
        assert ReconciliationResultKind.AMOUNT_MISMATCH == "amount_mismatch"

    def test_run_status_values(self) -> None:
        assert ReconciliationRunStatus.RUNNING == "running"
        assert ReconciliationRunStatus.COMPLETED == "completed"
        assert ReconciliationRunStatus.FAILED == "failed"

    def test_severity_values(self) -> None:
        from finance_sync.models.enums import ReconciliationSeverity

        assert ReconciliationSeverity.INFO == "info"
        assert ReconciliationSeverity.WARNING == "warning"
        assert ReconciliationSeverity.ERROR == "error"


# ═══════════════════════════════════════════════════════════════════════
# ReconciliationService unit tests (mocked UoW)
# ═══════════════════════════════════════════════════════════════════════


class _MockTxn:
    """Minimal transaction-like object for testing."""

    def __init__(
        self,
        *,
        id: str | None = None,
        tenant_id: str = "tenant_1",
        provider_key: str = "bunq",
        external_transaction_id: str = "ext_1",
        account_id: str = "acct_1",
        amount: Decimal = Decimal("-10.00"),
        currency_code: str = "EUR",
        occurred_at: datetime | None = None,
        description: str | None = None,
        transaction_type: str = "payment",
        status: str = "booked",
    ):
        self.id = id or str(uuid4())
        self.tenant_id = tenant_id
        self.provider_key = provider_key
        self.external_transaction_id = external_transaction_id
        self.account_id = account_id
        self.amount = amount
        self.currency_code = currency_code
        self.occurred_at = occurred_at or datetime.now(UTC)
        self.description = description
        self.transaction_type = transaction_type
        self.status = status


class _MockAccount:
    def __init__(self, *, id: str, name: str, tenant_id: str = "tenant_1"):
        self.id = id
        self.name = name
        self.tenant_id = tenant_id


class TestReconciliationServiceMocked:
    """Test ReconciliationService with a fully mocked UnitOfWork."""

    @pytest.fixture
    def session_factory(self):
        return MagicMock()

    @pytest.fixture
    def tenant_id(self):
        return "tenant_test_1"

    @pytest.fixture
    def now(self):
        return datetime.now(UTC)

    @pytest.fixture
    def svc(self, session_factory, tenant_id):
        from finance_sync.services.reconciliation import ReconciliationService

        return ReconciliationService(
            session_factory=session_factory,
            tenant_id=tenant_id,
        )

    async def test_detect_duplicates_empty(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """No duplicates when find_duplicate_candidates returns empty."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=None)
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(return_value=[])

        from finance_sync.models.reconciliation import ReconciliationRun

        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 0

    async def test_detect_duplicates_finds_pairs(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Duplicate detection returns findings for matched pairs."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="bunq_txn_1",
            account_id="acct_1",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=2),
            description="Groceries",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t212_txn_1",
            account_id="acct_1",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=3),  # 1 hour apart
            description="Groceries",
        )

        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=None)
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )

        from finance_sync.models.reconciliation import ReconciliationRun

        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.kind == ReconciliationResultKind.DUPLICATE_TRANSACTION
        assert f.provider_key == "bunq"
        assert f.other_provider_key == "trading212"
        assert f.description is not None
        assert "Groceries" in f.description
        assert f.details is not None
        assert f.details.get("confidence", 0) >= 0.5

    async def test_detect_cross_connector_gaps(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Cross-connector gap detection flags providers with limited range."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = AsyncMock()

        # Mock the raw session execute for accounts query
        mock_accounts_result = MagicMock()
        mock_accounts_result.scalars.return_value.all = MagicMock(
            return_value=[_MockAccount(id="acct_1", name="Test Account")]
        )
        mock_session.execute = AsyncMock(return_value=mock_accounts_result)

        # Single UoW mock that handles all contexts
        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=None)

        # Providers call
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )

        # Date range calls: first for bunq, then for trading212
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=30), now),  # bunq: full range
                (now - timedelta(days=15), now),  # trading212: starts later
            ]
        )

        from finance_sync.models.reconciliation import ReconciliationRun

        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) >= 1
        f = findings[0]
        assert f.kind in (
            ReconciliationResultKind.MISSING_TRANSACTION,
            ReconciliationResultKind.CROSS_CONNECTOR_MISMATCH,
        )

    async def test_list_runs(
        self, svc, session_factory
    ) -> None:
        """list_runs returns runs from the DB."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all = MagicMock(
            return_value=[MagicMock(id="run_1", tenant_id="tenant_test_1")]
        )
        mock_session.execute = AsyncMock(return_value=mock_result)
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        runs = await svc.list_runs()

        assert len(runs) == 1

    async def test_get_run_with_results_not_found(
        self, svc, session_factory
    ) -> None:
        """get_run_with_results returns None for non-existent run."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results("nonexistent")

        assert run is None
        assert results == []
        assert total == 0


# ═══════════════════════════════════════════════════════════════════════
# TransactionRepository duplicate candidate logic (pure Python, no DB)
# ═══════════════════════════════════════════════════════════════════════


class TestDuplicateCandidateLogic:
    """Test the grouping/matching logic that find_duplicate_candidates uses,
    exercised via a lightweight in-memory test."""
    # This tests the algorithm from Finance_sync/db/repositories.py
    # by directly testing the grouping and pair matching logic.

    @staticmethod
    def _find_pairs(txns: list, threshold_hours: int = 48) -> list:
        """Replicate the core pair-finding logic from find_duplicate_candidates."""
        from collections import defaultdict

        groups: dict[tuple[str, str], list] = defaultdict(list)
        for t in txns:
            key = (str(t.account_id), str(t.amount))
            groups[key].append(t)

        pairs = []
        for _key, group in groups.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda t: t.occurred_at or datetime(1970, 1, 1))
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if (
                        a.provider_key == b.provider_key
                        and a.external_transaction_id == b.external_transaction_id
                    ):
                        continue
                    t_a = a.occurred_at
                    t_b = b.occurred_at
                    if t_a is None or t_b is None:
                        continue
                    diff_hours = abs((t_a - t_b).total_seconds()) / 3600
                    if diff_hours <= threshold_hours:
                        pairs.append((a, b))

        pairs.sort(key=lambda p: abs(p[0].amount or 0), reverse=True)
        return pairs

    def test_no_duplicates_when_all_distinct(self) -> None:
        now = datetime.now(UTC)
        txns = [
            _MockTxn(amount=Decimal("-10.00"), occurred_at=now - timedelta(hours=2)),
            _MockTxn(amount=Decimal("-20.00"), occurred_at=now - timedelta(hours=6)),
            _MockTxn(amount=Decimal("-30.00"), occurred_at=now - timedelta(hours=24)),
        ]
        pairs = self._find_pairs(txns)
        assert len(pairs) == 0

    def test_detects_duplicate_same_amount_close_time(self) -> None:
        now = datetime.now(UTC)
        txns = [
            _MockTxn(
                provider_key="bunq",
                external_transaction_id="b1",
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=2),
            ),
            _MockTxn(
                provider_key="trading212",
                external_transaction_id="t1",
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=3),
            ),
        ]
        pairs = self._find_pairs(txns)
        assert len(pairs) == 1
        a, b = pairs[0]
        # The pair is sorted by occurred_at, so 'a' is the earlier one (t1, 3h ago)
        # and 'b' is the later one (b1, 2h ago)
        assert {a.external_transaction_id, b.external_transaction_id} == {"b1", "t1"}

    def test_ignores_same_provider_same_id(self) -> None:
        now = datetime.now(UTC)
        txns = [
            _MockTxn(
                provider_key="bunq",
                external_transaction_id="b1",
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=2),
            ),
            _MockTxn(
                provider_key="bunq",
                external_transaction_id="b1",  # Same external ID!
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=3),
            ),
        ]
        pairs = self._find_pairs(txns)
        assert len(pairs) == 0, "Should skip same provider+same external ID"

    def test_threshold_hours_filters(self) -> None:
        now = datetime.now(UTC)
        txns = [
            _MockTxn(
                provider_key="bunq",
                external_transaction_id="b1",
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=48),
            ),
            _MockTxn(
                provider_key="trading212",
                external_transaction_id="t1",
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=2),  # 46 hours apart
            ),
        ]
        # Threshold of 24 hours should filter it out
        pairs = self._find_pairs(txns, threshold_hours=24)
        assert len(pairs) == 0

        # Threshold of 48 hours should include it
        pairs = self._find_pairs(txns, threshold_hours=48)
        assert len(pairs) == 1

    def test_different_amounts_not_duplicates(self) -> None:
        now = datetime.now(UTC)
        txns = [
            _MockTxn(amount=Decimal("-50.00"), occurred_at=now - timedelta(hours=2)),
            _MockTxn(amount=Decimal("-51.00"), occurred_at=now - timedelta(hours=3)),
        ]
        pairs = self._find_pairs(txns)
        assert len(pairs) == 0

    def test_sorts_by_amount_descending(self) -> None:
        now = datetime.now(UTC)
        txns = [
            _MockTxn(
                provider_key="a",
                external_transaction_id="x1",
                amount=Decimal("-500.00"),
                occurred_at=now - timedelta(hours=2),
            ),
            _MockTxn(
                provider_key="b",
                external_transaction_id="y1",
                amount=Decimal("-500.00"),
                occurred_at=now - timedelta(hours=3),
            ),
            _MockTxn(
                provider_key="a",
                external_transaction_id="x2",
                amount=Decimal("-5.00"),
                occurred_at=now - timedelta(hours=1),
            ),
            _MockTxn(
                provider_key="b",
                external_transaction_id="y2",
                amount=Decimal("-5.00"),
                occurred_at=now - timedelta(hours=4),
            ),
        ]
        pairs = self._find_pairs(txns)
        assert len(pairs) == 2
        # First pair should be the larger amount
        assert pairs[0][0].amount == Decimal("-500.00")
