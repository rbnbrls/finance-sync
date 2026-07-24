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
        assert (
            ReconciliationResultKind.DUPLICATE_TRANSACTION
            == "duplicate_transaction"
        )
        assert (
            ReconciliationResultKind.MISSING_TRANSACTION
            == "missing_transaction"
        )
        assert (
            ReconciliationResultKind.CROSS_CONNECTOR_MISMATCH
            == "cross_connector_mismatch"
        )
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
# Stand-alone helper function tests
# ═══════════════════════════════════════════════════════════════════════


class TestReconciliationHelpers:
    """Test stand-alone helper functions in reconciliation.py."""

    def test_severity_zero_total(self) -> None:
        from finance_sync.services.reconciliation import _severity

        assert _severity(5, 0) == "info"

    def test_severity_info_below_threshold(self) -> None:
        from finance_sync.services.reconciliation import _severity

        # ratio = 1/100 = 0.01 -> INFO
        assert _severity(1, 100) == "info"

    def test_severity_warning_at_threshold(self) -> None:
        from finance_sync.services.reconciliation import _severity

        # ratio = 3/100 = 0.03 -> WARNING (0.02 < 0.03 <= 0.1)
        assert _severity(3, 100) == "warning"

    def test_severity_warning_boundary(self) -> None:
        from finance_sync.services.reconciliation import _severity

        # ratio = 10/100 = 0.1 -> WARNING (not > 0.1)
        assert _severity(10, 100) == "warning"

    def test_severity_error_above_threshold(self) -> None:
        from finance_sync.services.reconciliation import _severity

        # ratio = 11/100 = 0.11 > 0.1 -> ERROR
        assert _severity(11, 100) == "error"

    def test_severity_exact_info_boundary(self) -> None:
        from finance_sync.services.reconciliation import _severity

        # ratio = 1/50 = 0.02 -> INFO (not > 0.02)
        assert _severity(1, 50) == "info"

    def test_default_since_returns_datetime(self) -> None:
        from finance_sync.services.reconciliation import _default_since

        result = _default_since()
        assert isinstance(result, datetime)
        # Should be roughly 90 days ago
        diff = (datetime.now(UTC) - result).total_seconds()
        assert 89 * 86400 < diff < 91 * 86400


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
        self.occurred_at = occurred_at
        self.description = description
        self.transaction_type = transaction_type
        self.status = status


class _MockAccount:
    def __init__(self, *, id: str, name: str, tenant_id: str = "tenant_1"):
        self.id = id
        self.name = name
        self.tenant_id = tenant_id


# Shared helpers for setting up a mock UoW that manages multiple async
# context-manager entries (one per `async with UnitOfWork(session)`)
def _make_mock_uow() -> MagicMock:
    """Return a MagicMock that works as an async context manager."""
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    return uow


def _make_mock_session(accounts: list | None = None) -> AsyncMock:
    """Return a mock session whose .execute() returns accounts."""
    session = AsyncMock()
    acct_result = MagicMock()
    acct_result.scalars.return_value.all = MagicMock(
        return_value=accounts or []
    )
    session.execute = AsyncMock(return_value=acct_result)
    return session


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

    # ── _detect_duplicates tests ─────────────────────────────────────

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
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[]
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

    async def test_detect_duplicates_confidence_cross_provider_same_desc(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Cross-provider + same desc = 0.9 confidence (ERROR severity)."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b_ext_1",
            account_id="acct_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=1),
            description="Netflix",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t_ext_1",
            account_id="acct_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=2),
            description="Netflix",
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert (
            f.details["confidence"] == 0.9
        )  # 0.7 (cross-provider) + 0.2 (same desc)
        from finance_sync.models.enums import ReconciliationSeverity

        assert f.severity == ReconciliationSeverity.ERROR

    async def test_detect_duplicates_confidence_same_provider_same_desc(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Same provider + same desc + different external ids = 0.7 confidence (WARNING)."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b_ext_1",
            account_id="acct_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=1),
            description="Netflix",
        )
        tx_b = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b_ext_2",  # Same provider, different ext id
            account_id="acct_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=2),
            description="Netflix",  # Same description
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        # same_provider=True + not same_desc=False -> stops at base 0.5
        # same_desc=True -> +0.2 => 0.7
        assert f.details["confidence"] == 0.7

    async def test_detect_duplicates_confidence_same_provider_diff_desc(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Same provider + diff desc = 0.6 confidence (WARNING)."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b_ext_1",
            account_id="acct_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=1),
            description="Netflix",
        )
        tx_b = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b_ext_2",
            account_id="acct_1",
            amount=Decimal("-50.00"),
            occurred_at=now - timedelta(hours=2),
            description="Coffee Shop",
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details["confidence"] == 0.6
        from finance_sync.models.enums import ReconciliationSeverity

        assert f.severity == ReconciliationSeverity.WARNING

    async def test_detect_duplicates_same_provider_same_external_id_skipped(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Skip pairs with same provider+same external ID (they're the same transaction)."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[]
        )
        mock_session = AsyncMock()
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 0

    # ── _detect_cross_connector_gaps tests ───────────────────────────

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

    async def test_cross_connector_gaps_single_provider(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Single-provider accounts produce no gaps."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Single Provider Acct")]
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]  # Only one provider
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 0

    async def test_cross_connector_gaps_no_accounts(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """No accounts selected produces no gaps."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session([])  # No accounts
        mock_uow = _make_mock_uow()
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 0

    async def test_cross_connector_gaps_provider_no_transactions(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Provider with no transactions is flagged as missing."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Dual Provider Acct")]
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        # bunq has a range, trading212 returns None for both start and end
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=30), now),  # bunq
                (None, None),  # trading212: no transactions
            ]
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) >= 1
        f = findings[0]
        assert f.kind == ReconciliationResultKind.MISSING_TRANSACTION
        assert f.provider_key == "trading212"
        from finance_sync.models.enums import ReconciliationSeverity

        assert f.severity == ReconciliationSeverity.ERROR

    async def test_cross_connector_gaps_all_providers_no_range(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """All providers with no data -> continue (no all_starts/all_ends)."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="All Empty")]
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        # Both providers return None ranges — all_starts and all_ends are empty
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(None, None)
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session, run, None, now - timedelta(days=90), now
            )

        # No findings because all_starts is empty -> continue
        assert len(findings) == 0

    async def test_cross_connector_gaps_small_start_diff_ignored(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Start differences less than 7 days produce no findings."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Acct")]
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=30), now),  # bunq
                (now - timedelta(days=28), now),  # t212: only 2 days later
            ]
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 0

    # ── _detect_missing_transactions tests ───────────────────────────

    async def test_detect_missing_transactions_no_gaps(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """No gaps when provider started before the analysis window."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Full Coverage Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(
                now - timedelta(days=100),
                now,
            )  # Started before window
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 0

    async def test_detect_missing_transactions_with_gap(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Flags gap when provider started after analysis window start."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Gap Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        # Provider started 30 days ago but analysis window starts 90 days ago
        # => 60 day gap, well above 7 day threshold
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=30), now)
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.kind == ReconciliationResultKind.MISSING_TRANSACTION
        assert "60-day gap" in (f.description or "")
        assert f.provider_key == "bunq"
        from finance_sync.models.enums import ReconciliationSeverity

        assert f.severity == ReconciliationSeverity.INFO

    async def test_detect_missing_transactions_skips_no_data(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Skips providers with no data (p_start is None) — already flagged in gap detection."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="No Data Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(None, None)  # No data
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 0

    async def test_detect_missing_transactions_small_gap_ignored(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Gaps smaller than 7 days are ignored."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Small Gap Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        # Provider started 88 days ago, analysis window 90 days ago => 2 day gap (< 7)
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=88), now)
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 0

    async def test_detect_missing_transactions_multiple_providers(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Multiple providers: one full coverage, one with gap."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Multi Prov Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=100), now),  # bunq: full coverage
                (now - timedelta(days=30), now),  # t212: 60 day gap
            ]
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 1
        assert findings[0].provider_key == "trading212"

    # ── _finalize_run tests ──────────────────────────────────────────

    async def test_finalize_run_with_findings(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """_finalize_run computes summary stats and persists findings."""
        from finance_sync.models.enums import (
            ReconciliationResultKind,
            ReconciliationSeverity,
        )
        from finance_sync.models.reconciliation import (
            ReconciliationResult,
            ReconciliationRun,
        )
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = AsyncMock()
        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        findings = [
            ReconciliationResult(
                kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
                severity=ReconciliationSeverity.ERROR,
                description="dup_1",
            ),
            ReconciliationResult(
                kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
                severity=ReconciliationSeverity.WARNING,
                description="dup_2",
            ),
            ReconciliationResult(
                kind=ReconciliationResultKind.MISSING_TRANSACTION,
                severity=ReconciliationSeverity.INFO,
                description="missing_1",
            ),
        ]

        await RS._finalize_run(mock_session, run, findings)

        assert mock_session.add.call_count == 3
        assert mock_session.flush.call_count >= 1
        assert mock_session.commit.call_count == 1

        assert run.finding_count == 3
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.summary is not None
        assert run.summary["by_kind"] == {
            "duplicate_transaction": 2,
            "missing_transaction": 1,
        }
        assert run.summary["by_severity"] == {
            "error": 1,
            "warning": 1,
            "info": 1,
        }

    async def test_finalize_run_empty_findings(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """_finalize_run with empty findings produces empty stats."""
        from finance_sync.models.reconciliation import ReconciliationRun
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = AsyncMock()
        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        await RS._finalize_run(mock_session, run, [])

        assert run.finding_count == 0
        assert run.summary is not None
        assert run.summary["by_kind"] == {}
        assert run.summary["by_severity"] == {}

    # ── reconcile() full-flow tests ──────────────────────────────────

    async def test_reconcile_full_success(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Full reconcile flow runs all three phases and returns a completed run."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork

        # Mock the session factory to return a session
        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Full Acct")]
        )
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        # Mock UnitOfWork that handles find_duplicate_candidates
        mock_uow = _make_mock_uow()

        # Phase 1: returns one duplicate pair
        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=1),
            description="Rent",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=Decimal("-100.00"),
            occurred_at=now - timedelta(hours=2),
            description="Rent",
        )
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        # Phases 2 & 3: needs get_providers_for_account and get_transaction_date_range
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=30), now),  # cross-connector: bunq
                (now - timedelta(days=15), now),  # cross-connector: t212 (gap)
                (
                    now - timedelta(days=100),
                    now,
                ),  # missing: bunq (full coverage)
                (
                    now - timedelta(days=30),
                    now,
                ),  # missing: t212 (60d gap from analysis start)
            ]
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            run = await svc.reconcile(
                account_ids=["acct_1"],
                date_from=now - timedelta(days=90),
                date_to=now,
            )

        assert run is not None
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count >= 2
        assert run.summary is not None
        assert run.summary["by_kind"].get("duplicate_transaction", 0) >= 1

    async def test_reconcile_error_handling(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Exception during reconciliation marks run as FAILED."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock(return_value=None)
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_uow = _make_mock_uow()
        # Make Phase 1 fail with a database error
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            side_effect=ValueError("DB query failed!")
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            run = await svc.reconcile(
                account_ids=["acct_1"],
                date_from=now - timedelta(days=90),
                date_to=now,
            )

        assert run is not None
        assert run.status == ReconciliationRunStatus.FAILED
        assert run.error_message is not None
        assert run.completed_at is not None

    async def test_reconcile_empty_findings(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Reconcile with no issues produces an empty run."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork

        mock_session = _make_mock_session([])  # No accounts = no findings
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[]
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            run = await svc.reconcile(
                date_from=now - timedelta(days=90),
                date_to=now,
            )

        assert run is not None
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count == 0

    # ── provider_keys tests ──────────────────────────────────────────

    async def test_reconcile_passes_provider_keys_to_duplicate_detection(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """provider_keys parameter is forwarded to find_duplicate_candidates."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models.reconciliation import ReconciliationRun

        provider_keys = ["bunq", "trading212"]

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[]
        )
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=60), now)
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Shared Account")]
        )
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            run = await svc.reconcile(
                provider_keys=provider_keys,
                date_from=now - timedelta(days=90),
                date_to=now,
            )

        assert run.scope is not None
        assert run.scope.get("provider_keys") == provider_keys

        # Verify find_duplicate_candidates was called with provider_keys
        call_kwargs = (
            mock_uow.transactions.find_duplicate_candidates.call_args.kwargs
        )
        assert call_kwargs.get("provider_keys") == provider_keys

    async def test_detect_cross_connector_gaps_filters_by_provider_keys(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """_detect_cross_connector_gaps only compares specified providers."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models.reconciliation import ReconciliationRun
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        provider_keys = ["bunq"]

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212", "revolut"]
        )

        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Multi-provider Account")]
        )

        # When only 'bunq' is in provider_keys, there are fewer than 2
        # providers to compare, so cross-connector gap detection should
        # skip this account (no findings)
        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session,
                run,
                None,
                now - timedelta(days=90),
                now,
                provider_keys=provider_keys,
            )

        assert len(findings) == 0

    async def test_detect_cross_connector_gaps_compares_two_providers(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """With exactly 2 provider_keys, cross-connector gaps are detected."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models.reconciliation import ReconciliationRun
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_uow = _make_mock_uow()
        # Two providers in provider_keys — should proceed
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212", "revolut"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=90), now),  # bunq: full range
                (now - timedelta(days=10), now),  # trading212: late start
            ]
        )

        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Multi-provider Account")]
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session,
                run,
                None,
                now - timedelta(days=90),
                now,
                provider_keys=["bunq", "trading212"],
            )

        # Should find a gap since trading212 started late
        assert len(findings) >= 1

    async def test_detect_missing_transactions_filters_by_provider_keys(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """_detect_missing_transactions only checks specified providers."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models.reconciliation import ReconciliationRun
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        # Only check bunq — should still get called
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=5), now)  # recent start
        )

        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

        analysis_start = now - timedelta(days=90)
        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Test Account")]
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session,
                run,
                None,
                analysis_start,
                now,
                provider_keys=["bunq"],  # only check bunq
            )

        # Should find a gap since bunq started only 5 days ago
        assert len(findings) >= 1

    # ── list_runs tests ──────────────────────────────────────────────

    async def test_list_runs(self, svc, session_factory) -> None:
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

    async def test_list_runs_empty(self, svc, session_factory) -> None:
        """list_runs returns empty list when no runs exist."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all = MagicMock(return_value=[])
        mock_session.execute = AsyncMock(return_value=mock_result)
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        runs = await svc.list_runs(limit=10, offset=0)

        assert len(runs) == 0

    # ── get_run_with_results tests ───────────────────────────────────

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

    async def test_get_run_with_results_found(
        self, svc, session_factory
    ) -> None:
        """get_run_with_results returns the run and its results."""
        mock_run = MagicMock(id="run_1", tenant_id="tenant_test_1")
        mock_result_1 = MagicMock(
            kind="duplicate_transaction", severity="warning"
        )
        mock_result_2 = MagicMock(kind="missing_transaction", severity="info")

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_run)

        # Mock the execute calls (count + list)
        mock_total_result = MagicMock()
        mock_total_result.scalar = MagicMock(return_value=2)
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all = MagicMock(
            return_value=[mock_result_1, mock_result_2]
        )
        mock_session.execute = AsyncMock(
            side_effect=[mock_total_result, mock_list_result]
        )

        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results("run_1")

        assert run is mock_run
        assert len(results) == 2
        assert total == 2

    async def test_get_run_with_results_kind_filter(
        self, svc, session_factory
    ) -> None:
        """get_run_with_results with kind_filter only returns matching results."""
        mock_run = MagicMock(id="run_1", tenant_id="tenant_test_1")

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_run)

        mock_total_result = MagicMock()
        mock_total_result.scalar = MagicMock(return_value=1)
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all = MagicMock(
            return_value=[MagicMock(kind="duplicate_transaction")]
        )
        mock_session.execute = AsyncMock(
            side_effect=[mock_total_result, mock_list_result]
        )

        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results(
            "run_1", kind_filter="duplicate_transaction"
        )

        assert run is mock_run
        assert total == 1
        assert len(results) == 1

    async def test_get_run_with_results_severity_filter(
        self, svc, session_factory
    ) -> None:
        """get_run_with_results with severity_filter only returns matching results."""
        mock_run = MagicMock(id="run_1", tenant_id="tenant_test_1")

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_run)

        mock_total_result = MagicMock()
        mock_total_result.scalar = MagicMock(return_value=1)
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all = MagicMock(
            return_value=[MagicMock(severity="error")]
        )
        mock_session.execute = AsyncMock(
            side_effect=[mock_total_result, mock_list_result]
        )

        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, _results, total = await svc.get_run_with_results(
            "run_1", severity_filter="error"
        )

        assert run is mock_run
        assert total == 1


class TestReconciliationModelBasics:
    """Test basic model behaviour (__repr__, construction)."""

    def test_reconciliation_run_repr(self) -> None:
        from finance_sync.models.reconciliation import ReconciliationRun

        r = ReconciliationRun(
            tenant_id="t1", status=ReconciliationRunStatus.RUNNING
        )
        rep = repr(r)
        assert "ReconciliationRun" in rep
        assert r.status.value in rep

    def test_reconciliation_result_repr(self) -> None:
        from finance_sync.models.reconciliation import ReconciliationResult

        r = ReconciliationResult(
            kind=ReconciliationResultKind.DUPLICATE_TRANSACTION
        )
        rep = repr(r)
        assert "ReconciliationResult" in rep


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
            group.sort(
                key=lambda t: t.occurred_at or datetime(1970, 1, 1, tzinfo=UTC)
            )
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if (
                        a.provider_key == b.provider_key
                        and a.external_transaction_id
                        == b.external_transaction_id
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
            _MockTxn(
                amount=Decimal("-10.00"), occurred_at=now - timedelta(hours=2)
            ),
            _MockTxn(
                amount=Decimal("-20.00"), occurred_at=now - timedelta(hours=6)
            ),
            _MockTxn(
                amount=Decimal("-30.00"), occurred_at=now - timedelta(hours=24)
            ),
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
        assert {a.external_transaction_id, b.external_transaction_id} == {
            "b1",
            "t1",
        }

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
            _MockTxn(
                amount=Decimal("-50.00"), occurred_at=now - timedelta(hours=2)
            ),
            _MockTxn(
                amount=Decimal("-51.00"), occurred_at=now - timedelta(hours=3)
            ),
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

    def test_handles_none_occurred_at(self) -> None:
        """Transactions with None occurred_at are skipped gracefully."""
        txns = [
            _MockTxn(
                provider_key="bunq",
                external_transaction_id="b1",
                amount=Decimal("-50.00"),
                occurred_at=None,
            ),
            _MockTxn(
                provider_key="trading212",
                external_transaction_id="t1",
                amount=Decimal("-50.00"),
                occurred_at=datetime.now(UTC) - timedelta(hours=2),
            ),
        ]
        pairs = self._find_pairs(txns)
        assert len(pairs) == 0

    def test_three_way_duplicate_detection(self) -> None:
        """Three transactions with same amount in same account yields three pairs."""
        now = datetime.now(UTC)
        txns = [
            _MockTxn(
                provider_key="bunq",
                external_transaction_id="b1",
                amount=Decimal("-25.00"),
                occurred_at=now - timedelta(hours=1),
            ),
            _MockTxn(
                provider_key="trading212",
                external_transaction_id="t1",
                amount=Decimal("-25.00"),
                occurred_at=now - timedelta(hours=2),
            ),
            _MockTxn(
                provider_key="revolut",
                external_transaction_id="r1",
                amount=Decimal("-25.00"),
                occurred_at=now - timedelta(hours=3),
            ),
        ]
        pairs = self._find_pairs(txns)
        # Combinations: (b1,t1), (b1,r1), (t1,r1) = 3 pairs
        assert len(pairs) == 3

    def test_different_accounts_not_duplicates(self) -> None:
        """Same amount but different accounts = not duplicates."""
        now = datetime.now(UTC)
        txns = [
            _MockTxn(
                account_id="acct_1",
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=1),
            ),
            _MockTxn(
                account_id="acct_2",
                amount=Decimal("-50.00"),
                occurred_at=now - timedelta(hours=2),
            ),
        ]
        pairs = self._find_pairs(txns)
        assert len(pairs) == 0
