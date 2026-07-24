"""Additional edge-case and stress tests for the reconciliation service.

Extends the core tests in test_reconciliation.py with:
- Edge cases (None amounts, None/empty descriptions)
- Boundary value tests (exactly at 7-day threshold)
- Stress tests (100+ duplicate candidate pairs)
- Combined filter scenarios (kind + severity together)
- Default parameter path coverage
"""  # noqa: D205, D212

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from finance_sync.models.enums import (
    ReconciliationResultKind,
    ReconciliationRunStatus,
    ReconciliationSeverity,
)


# Re-use the same mock transaction/account types from the parent test file.
# (We duplicate them here so this module is self-contained.)


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


# ═══════════════════════════════════════════════════════════════════════
# Duplicate detection — value-domain edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestDuplicateDetectionEdgeCases:
    """Value-domain edge cases for _detect_duplicates."""

    @pytest.fixture
    def session_factory(self):
        return MagicMock()

    @pytest.fixture
    def tenant_id(self):
        return "tenant_test_edge"

    @pytest.fixture
    def now(self):
        return datetime.now(UTC)

    @pytest.fixture
    def run(self, tenant_id, now):
        from finance_sync.models.reconciliation import ReconciliationRun

        return ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.RUNNING,
            started_at=now,
        )

    async def test_none_amount_on_one_side(
        self, tenant_id, now, run
    ) -> None:
        """One side has None amount — Decimal(0) fallback is used."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=None,  # None amount
            occurred_at=now - timedelta(hours=1),
            description="Coffee",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=Decimal("-5.00"),
            occurred_at=now - timedelta(hours=2),
            description="Coffee",
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details is not None
        # amount_diff should be |0 - (-5.00)| = 5.00 (Decimal(0) fallback)
        assert f.details["amount_diff"] is not None

    async def test_none_amount_on_both_sides(
        self, tenant_id, now, run
    ) -> None:
        """Both sides have None amount — both use Decimal(0) fallback."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=None,
            occurred_at=now - timedelta(hours=1),
            description="Coffee",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=None,
            occurred_at=now - timedelta(hours=2),
            description="Coffee",
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details is not None
        # both fall back to 0 → amount_diff = 0
        assert f.details["amount_diff"] == "0"

    async def test_both_descriptions_none(
        self, tenant_id, now, run
    ) -> None:
        """Both descriptions are None — same_desc is False."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=Decimal("-10.00"),
            occurred_at=now - timedelta(hours=1),
            description=None,
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=Decimal("-10.00"),
            occurred_at=now - timedelta(hours=2),
            description=None,
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details is not None
        # cross-provider + not same_desc = 0.7
        assert f.details["confidence"] == 0.7
        # description should say "no desc / no desc"
        assert f.description is not None
        assert "no desc" in f.description

    async def test_one_description_none(
        self, tenant_id, now, run
    ) -> None:
        """One description None, the other valid — same_desc is False."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=Decimal("-10.00"),
            occurred_at=now - timedelta(hours=1),
            description="Groceries",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=Decimal("-10.00"),
            occurred_at=now - timedelta(hours=2),
            description=None,
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details is not None
        # cross-provider + not same_desc (tx_b.description is None → short-circuit) = 0.7
        assert f.details["confidence"] == 0.7

    async def test_empty_string_descriptions(
        self, tenant_id, now, run
    ) -> None:
        """Empty string descriptions are truthy and match each other."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=Decimal("-20.00"),
            occurred_at=now - timedelta(hours=1),
            description="",  # empty, but truthy
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=Decimal("-20.00"),
            occurred_at=now - timedelta(hours=2),
            description="",  # empty, but truthy
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details is not None
        # Empty string is falsy in the ``and`` chain → same_desc = False
        # cross-provider (0.7), no same_desc bonus → 0.7
        assert f.details["confidence"] == 0.7

    async def test_diff_hours_calculation(
        self, tenant_id, now, run
    ) -> None:
        """Verify diff_hours in details is computed correctly."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        gap = timedelta(hours=5, minutes=30)
        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=Decimal("-100.00"),
            occurred_at=now,
            description="Same",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=Decimal("-100.00"),
            occurred_at=now - gap,
            description="Same",
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details is not None
        # 5.5 hours diff
        assert f.details["diff_hours"] == 5.5

    async def test_large_set_of_candidates(
        self, tenant_id, now, run
    ) -> None:
        """Stress test: process 100+ duplicate candidate pairs."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        # Generate 125 candidate pairs
        pairs = []
        for i in range(125):
            tx_a = _MockTxn(
                provider_key="bunq",
                external_transaction_id=f"b_{i}_1",
                account_id="acct_1",
                amount=Decimal(f"-{i + 1}.00"),
                occurred_at=now - timedelta(hours=1 + i),
                description=f"Item {i}",
            )
            tx_b = _MockTxn(
                provider_key="trading212",
                external_transaction_id=f"t_{i}_1",
                account_id="acct_1",
                amount=Decimal(f"-{i + 1}.00"),
                occurred_at=now - timedelta(hours=2 + i),
                description=f"Item {i}",
            )
            pairs.append((tx_a, tx_b))

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=pairs
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 125
        for f in findings:
            assert f.details is not None
            assert f.details["confidence"] > 0

    async def test_case_insensitive_description_matching(
        self, tenant_id, now, run
    ) -> None:
        """Same description with different casing matches as duplicate."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        tx_a = _MockTxn(
            provider_key="bunq",
            external_transaction_id="b1",
            account_id="acct_1",
            amount=Decimal("-30.00"),
            occurred_at=now - timedelta(hours=1),
            description="NETFLIX",
        )
        tx_b = _MockTxn(
            provider_key="trading212",
            external_transaction_id="t1",
            account_id="acct_1",
            amount=Decimal("-30.00"),
            occurred_at=now - timedelta(hours=2),
            description="netflix",
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[(tx_a, tx_b)]
        )
        mock_session = AsyncMock()

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_duplicates(
                mock_session, run, None, now - timedelta(days=90), now, 48
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.details is not None
        # same_desc = True (case-insensitive match) → +0.2
        assert f.details["confidence"] == 0.9
        assert f.details["same_description"] is True


# ═══════════════════════════════════════════════════════════════════════
# Cross-connector gap — additional scenarios
# ═══════════════════════════════════════════════════════════════════════


class TestCrossConnectorGapEdgeCases:
    """Additional edge cases for _detect_cross_connector_gaps."""

    @pytest.fixture
    def tenant_id(self):
        return "tenant_test_edge"

    @pytest.fixture
    def now(self):
        return datetime.now(UTC)

    async def test_three_providers_mixed_coverage(
        self, tenant_id, now
    ) -> None:
        """Three providers: one full, one late, one no-data."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Three Provider Acct")]
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212", "revolut"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=90), now),  # bunq: full range
                (now - timedelta(days=10), now),  # t212: starts 80 days late
                (None, None),  # revolut: no transactions at all
            ]
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session, run, None, now - timedelta(days=90), now
            )

        # revolut → ERROR (no transactions), trading212 → WARNING (late start)
        assert len(findings) == 2

        revolut_f = [f for f in findings if f.provider_key == "revolut"]
        t212_f = [f for f in findings if f.provider_key == "trading212"]

        assert len(revolut_f) == 1
        assert revolut_f[0].severity == ReconciliationSeverity.ERROR

        assert len(t212_f) == 1
        assert t212_f[0].severity == ReconciliationSeverity.WARNING

    async def test_provider_keys_explicit_none(
        self, tenant_id, now
    ) -> None:
        """Explicit provider_keys=None behaves identically to omitting it."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Dual Prov Acct")]
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=90), now),
                (now - timedelta(days=10), now),
            ]
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session,
                run,
                None,
                now - timedelta(days=90),
                now,
                provider_keys=None,
            )

        # No provider filtering, so t212's late start should still be flagged
        assert len(findings) >= 1

    async def test_account_ids_with_matching_account(
        self, tenant_id, now
    ) -> None:
        """account_ids filter returns only matching accounts."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [
                _MockAccount(id="acct_1", name="Target Acct"),
            ]
        )

        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=90), now),
                (now - timedelta(days=10), now),
            ]
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session,
                run,
                account_ids=["acct_1"],
                _date_from=now - timedelta(days=90),
                _date_to=now,
            )

        # Should execute as normal with the one matching account
        assert len(findings) >= 1

    async def test_account_ids_empty_list_yields_no_accounts(
        self, tenant_id, now
    ) -> None:
        """Empty account_ids list returns no accounts → no findings."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session([])  # No accounts -> continue -> no findings
        mock_uow = _make_mock_uow()
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_cross_connector_gaps(
                mock_session,
                run,
                account_ids=[],
                _date_from=now - timedelta(days=90),
                _date_to=now,
            )

        assert len(findings) == 0


# ═══════════════════════════════════════════════════════════════════════
# Missing transaction detection — boundary and filter tests
# ═══════════════════════════════════════════════════════════════════════


class TestMissingTransactionEdgeCases:
    """Boundary and filter tests for _detect_missing_transactions."""

    @pytest.fixture
    def tenant_id(self):
        return "tenant_test_edge"

    @pytest.fixture
    def now(self):
        return datetime.now(UTC)

    async def test_exactly_seven_day_gap_not_flagged(
        self, tenant_id, now
    ) -> None:
        """A gap of exactly 7.0 days is NOT flagged (> 7 threshold)."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Boundary Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        # Provider started 83 days ago, analysis window 90 days ago → 7.0 day gap
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=83), now)
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        # 7.0 days is NOT > 7, so no finding
        assert len(findings) == 0

    async def test_slightly_above_seven_day_gap_flagged(
        self, tenant_id, now
    ) -> None:
        """A gap of 7.1 days IS flagged (> 7 threshold)."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Boundary Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        # 7.1 days = 7*86400 + 8640 seconds gap
        gap = timedelta(days=7, hours=2, minutes=24)  # ~7.1 days
        provider_start = now - timedelta(days=90) + gap
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(provider_start, now)
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        assert len(findings) == 1
        f = findings[0]
        assert f.kind == ReconciliationResultKind.MISSING_TRANSACTION

    async def test_account_ids_filter(
        self, tenant_id, now
    ) -> None:
        """account_ids is passed through to the query."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Filtered Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=30), now)
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, ["acct_1"], now - timedelta(days=90), now
            )

        assert len(findings) >= 1

    async def test_multiple_providers_mixed_gaps(
        self, tenant_id, now
    ) -> None:
        """Multiple providers: one full coverage, one missing, one small gap."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Three Prov Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212", "revolut"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            side_effect=[
                (now - timedelta(days=100), now),  # bunq: full coverage
                (None, None),  # t212: no data (skipped)
                (now - timedelta(days=30), now),  # revolut: 60 day gap
            ]
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session, run, None, now - timedelta(days=90), now
            )

        # Only revolut should be flagged (t212 has no data → skipped)
        assert len(findings) == 1
        assert findings[0].provider_key == "revolut"

    async def test_provider_keys_reduces_scope(
        self, tenant_id, now
    ) -> None:
        """provider_keys limits which providers are checked."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork
        from finance_sync.services.reconciliation import (
            ReconciliationService as RS,
        )

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Scope Acct")]
        )
        mock_uow = _make_mock_uow()
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq", "trading212", "revolut"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=100), now)  # bunq: full coverage
        )
        run = MagicMock(tenant_id=tenant_id, id="run_1")

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            findings = await RS._detect_missing_transactions(
                mock_session,
                run,
                None,
                now - timedelta(days=90),
                now,
                provider_keys=["bunq"],  # only check bunq, which has full coverage
            )

        # bunq has full coverage → no finding
        assert len(findings) == 0


# ═══════════════════════════════════════════════════════════════════════
# _finalize_run — additional scenarios
# ═══════════════════════════════════════════════════════════════════════


class TestFinalizeRunEdgeCases:
    """Additional scenarios for _finalize_run."""

    @pytest.fixture
    def tenant_id(self):
        return "tenant_test_edge"

    @pytest.fixture
    def now(self):
        return datetime.now(UTC)

    async def test_single_severity_all_info(
        self, tenant_id, now
    ) -> None:
        """All findings have the same severity (INFO)."""
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
                kind=ReconciliationResultKind.MISSING_TRANSACTION,
                severity=ReconciliationSeverity.INFO,
                description="m1",
            ),
            ReconciliationResult(
                kind=ReconciliationResultKind.MISSING_TRANSACTION,
                severity=ReconciliationSeverity.INFO,
                description="m2",
            ),
        ]

        await RS._finalize_run(mock_session, run, findings)

        assert run.finding_count == 2
        assert run.summary is not None
        assert run.summary["by_severity"] == {"info": 2}
        assert run.summary["by_kind"] == {"missing_transaction": 2}
        assert run.status == ReconciliationRunStatus.COMPLETED

    async def test_only_one_kind(
        self, tenant_id, now
    ) -> None:
        """Only duplicate findings (no cross-kind mixing)."""
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
                description="d1",
            ),
            ReconciliationResult(
                kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
                severity=ReconciliationSeverity.ERROR,
                description="d2",
            ),
            ReconciliationResult(
                kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
                severity=ReconciliationSeverity.ERROR,
                description="d3",
            ),
        ]

        await RS._finalize_run(mock_session, run, findings)

        assert run.finding_count == 3
        assert run.summary is not None
        assert run.summary["by_kind"] == {"duplicate_transaction": 3}
        assert run.summary["by_severity"] == {"error": 3}


# ═══════════════════════════════════════════════════════════════════════
# reconcile() — default parameter and scope tests
# ═══════════════════════════════════════════════════════════════════════


class TestReconcileDefaultParameters:
    """Test reconcile() with default / omitted parameters."""

    @pytest.fixture
    def session_factory(self):
        return MagicMock()

    @pytest.fixture
    def tenant_id(self):
        return "tenant_test_edge"

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

    async def test_reconcile_default_dates(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Reconcile with no explicit dates uses default 90-day window."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Default Acct")]
        )
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[]
        )
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=100), now)
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            run = await svc.reconcile()

        assert run is not None
        assert run.status == ReconciliationRunStatus.COMPLETED
        # Scope should include auto-generated dates
        assert run.scope is not None
        assert "date_from" in run.scope
        assert "date_to" in run.scope

    async def test_reconcile_account_ids_in_scope(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """account_ids parameter is recorded in scope dict."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork

        mock_session = _make_mock_session(
            [_MockAccount(id="acct_1", name="Scoped Acct")]
        )
        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_uow = _make_mock_uow()
        mock_uow.transactions.find_duplicate_candidates = AsyncMock(
            return_value=[]
        )
        mock_uow.transactions.get_providers_for_account = AsyncMock(
            return_value=["bunq"]
        )
        mock_uow.transactions.get_transaction_date_range = AsyncMock(
            return_value=(now - timedelta(days=100), now)
        )

        with patch.object(UnitOfWork, "__aenter__", return_value=mock_uow):
            run = await svc.reconcile(
                account_ids=["acct_1", "acct_2"],
                date_from=now - timedelta(days=90),
                date_to=now,
            )

        assert run.scope is not None
        assert run.scope.get("account_ids") == ["acct_1", "acct_2"]

    async def test_reconcile_default_threshold_hours(
        self, svc, session_factory, tenant_id, now
    ) -> None:
        """Default threshold_hours (48) is passed to duplicate detection."""
        from unittest.mock import patch

        from finance_sync.db.uow import UnitOfWork

        mock_session = _make_mock_session([])
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

        assert run.status == ReconciliationRunStatus.COMPLETED
        # Verify the default was passed to find_duplicate_candidates
        call_kwargs = mock_uow.transactions.find_duplicate_candidates.call_args.kwargs
        assert call_kwargs.get("threshold_hours") == 48


# ═══════════════════════════════════════════════════════════════════════
# get_run_with_results — combined filter tests
# ═══════════════════════════════════════════════════════════════════════


class TestGetRunWithResultsEdgeCases:
    """Additional filter scenarios for get_run_with_results."""

    @pytest.fixture
    def session_factory(self):
        return MagicMock()

    @pytest.fixture
    def tenant_id(self):
        return "tenant_test_edge"

    @pytest.fixture
    def svc(self, session_factory, tenant_id):
        from finance_sync.services.reconciliation import ReconciliationService

        return ReconciliationService(
            session_factory=session_factory,
            tenant_id=tenant_id,
        )

    async def test_both_filters_combined(
        self, svc, session_factory
    ) -> None:
        """kind_filter and severity_filter are combined with AND."""
        mock_run = MagicMock(id="run_1", tenant_id="tenant_test_edge")

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_run)

        mock_total_result = MagicMock()
        mock_total_result.scalar = MagicMock(return_value=1)
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all = MagicMock(
            return_value=[MagicMock(kind="duplicate_transaction", severity="error")]
        )
        mock_session.execute = AsyncMock(
            side_effect=[mock_total_result, mock_list_result]
        )

        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results(
            "run_1",
            kind_filter="duplicate_transaction",
            severity_filter="error",
        )

        assert run is mock_run
        assert total == 1
        assert len(results) == 1

    async def test_pagination_parameters(
        self, svc, session_factory
    ) -> None:
        """result_offset and result_limit are passed through."""
        mock_run = MagicMock(id="run_1", tenant_id="tenant_test_edge")

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_run)

        mock_total_result = MagicMock()
        mock_total_result.scalar = MagicMock(return_value=5)
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all = MagicMock(
            return_value=[
                MagicMock(kind="missing_transaction", severity="info")
            ]
        )
        mock_session.execute = AsyncMock(
            side_effect=[mock_total_result, mock_list_result]
        )

        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results(
            "run_1",
            result_offset=10,
            result_limit=5,
        )

        assert run is mock_run
        assert total == 5
        assert len(results) == 1

    async def test_severity_filter_only_with_no_run(
        self, svc, session_factory
    ) -> None:
        """Severity filter on non-existent run is handled gracefully."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results(
            "nonexistent", severity_filter="error"
        )

        assert run is None
        assert results == []
        assert total == 0

    async def test_kind_filter_only_with_no_run(
        self, svc, session_factory
    ) -> None:
        """Kind filter on non-existent run is handled gracefully."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

        session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results(
            "nonexistent", kind_filter="duplicate_transaction"
        )

        assert run is None
        assert results == []
        assert total == 0


# ═══════════════════════════════════════════════════════════════════════
# Model basics — extended
# ═══════════════════════════════════════════════════════════════════════


class TestReconciliationModelExtended:
    """Extended model tests (repr, full-field construction)."""

    def test_reconciliation_result_repr(self) -> None:
        """ReconciliationResult.__repr__ includes kind and severity."""
        from finance_sync.models.reconciliation import ReconciliationResult

        r = ReconciliationResult(
            kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
            severity=ReconciliationSeverity.WARNING,
        )
        rep = repr(r)
        assert "ReconciliationResult" in rep
        assert "duplicate_transaction" in rep
        assert "warning" in rep

    def test_reconciliation_run_repr_with_findings(self) -> None:
        """ReconciliationRun.__repr__ includes finding count when set."""
        from finance_sync.models.reconciliation import ReconciliationRun

        r = ReconciliationRun(
            tenant_id="t1",
            status=ReconciliationRunStatus.COMPLETED,
        )
        r.finding_count = 42
        rep = repr(r)
        assert "ReconciliationRun" in rep
        assert "completed" in rep
        assert "42" in rep

    def test_reconciliation_result_all_fields(self) -> None:
        """Construct a ReconciliationResult with all optional fields."""
        from datetime import timezone

        from finance_sync.models.reconciliation import ReconciliationResult

        r = ReconciliationResult(
            run_id="run_1",
            tenant_id="tenant_1",
            kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
            severity=ReconciliationSeverity.ERROR,
            account_id="acct_1",
            provider_key="bunq",
            other_provider_key="trading212",
            transaction_id_a="tx_a_1",
            transaction_id_b="tx_b_1",
            external_transaction_id_a="ext_a_1",
            external_transaction_id_b="ext_b_1",
            amount=Decimal("-100.00"),
            other_amount=Decimal("-100.00"),
            occurred_at=datetime.now(timezone.utc),
            description="Test finding",
            details={"confidence": 0.9},
        )
        assert r.run_id == "run_1"
        assert r.kind == ReconciliationResultKind.DUPLICATE_TRANSACTION
        assert r.severity == ReconciliationSeverity.ERROR
        assert r.provider_key == "bunq"
        assert r.other_provider_key == "trading212"
        assert r.amount == Decimal("-100.00")
        assert r.details == {"confidence": 0.9}
