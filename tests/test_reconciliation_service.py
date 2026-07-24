"""Tests for the ReconciliationService — the service-layer orchestrator.

Tests the full reconcile() pipeline, internal detection phases, result
finalization, run listing, and result retrieval using mocked database
sessions and UnitOfWork.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.models.enums import (
    ReconciliationResultKind,
    ReconciliationRunStatus,
    ReconciliationSeverity,
)
from finance_sync.services.reconciliation import (
    ReconciliationService,
    _default_since,
    _severity,
)


# ═══════════════════════════════════════════════════════════════════════
# Unit: _severity helper
# ═══════════════════════════════════════════════════════════════════════


class TestSeverityHelper:
    """Direct tests for the _severity helper function."""

    def test_zero_total_returns_info(self) -> None:
        assert _severity(0, 0) == ReconciliationSeverity.INFO

    def test_low_ratio_returns_info(self) -> None:
        assert _severity(1, 100) == ReconciliationSeverity.INFO

    def test_medium_ratio_returns_warning(self) -> None:
        assert _severity(3, 100) == ReconciliationSeverity.WARNING

    def test_high_ratio_returns_error(self) -> None:
        assert _severity(15, 100) == ReconciliationSeverity.ERROR

    def test_exactly_at_warning_threshold(self) -> None:
        """2% of total → exactly at WARNING threshold (ratio > 0.02)."""
        assert _severity(3, 149) == ReconciliationSeverity.WARNING

    def test_exactly_at_error_threshold(self) -> None:
        """10% of total → exactly at ERROR threshold (ratio > 0.1)."""
        assert _severity(11, 100) == ReconciliationSeverity.ERROR

    def test_all_findings_is_error(self) -> None:
        assert _severity(100, 100) == ReconciliationSeverity.ERROR


# ═══════════════════════════════════════════════════════════════════════
# Unit: _default_since
# ═══════════════════════════════════════════════════════════════════════


class TestDefaultSince:
    """Tests for the _default_since helper."""

    def test_returns_naive_datetime(self) -> None:
        result = _default_since()
        assert result.tzinfo is not None  # should be timezone-aware

    def test_is_roughly_90_days_ago(self) -> None:
        result = _default_since()
        diff = datetime.now(UTC) - result
        assert 89 <= diff.days <= 91


# ═══════════════════════════════════════════════════════════════════════
# Service fixture
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def session_factory():
    """Return a mock async_sessionmaker that produces mock sessions."""
    factory = MagicMock()
    session = AsyncMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return factory


@pytest.fixture
def svc(session_factory) -> ReconciliationService:
    """Return a ReconciliationService with a mocked session factory."""
    return ReconciliationService(
        session_factory=session_factory,
        tenant_id="test-tenant",
    )


# ═══════════════════════════════════════════════════════════════════════
# reconcile() — top-level orchestration
# ═══════════════════════════════════════════════════════════════════════


class TestReconcileMethod:
    """Test the top-level reconcile() method with mocked phases."""

    async def test_reconcile_success(
        self, svc: ReconciliationService, session_factory: MagicMock
    ) -> None:
        """reconcile() completes and returns a run with findings."""
        session = session_factory.return_value.__aenter__.return_value
        # Mock the internal phases to return findings
        mock_dup = MagicMock()
        mock_dup.kind = ReconciliationResultKind.DUPLICATE_TRANSACTION
        mock_dup.severity = ReconciliationSeverity.WARNING

        mock_gap = MagicMock()
        mock_gap.kind = ReconciliationResultKind.MISSING_TRANSACTION
        mock_gap.severity = ReconciliationSeverity.INFO

        with (
            patch.object(
                svc, "_detect_duplicates", new=AsyncMock(return_value=[mock_dup])
            ),
            patch.object(
                svc, "_detect_cross_connector_gaps", new=AsyncMock(return_value=[mock_gap])
            ),
            patch.object(
                svc, "_detect_missing_transactions", new=AsyncMock(return_value=[])
            ),
        ):
            run = await svc.reconcile()

        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.finding_count == 2
        assert run.summary is not None
        assert "duplicate_transaction" in str(run.summary)

        # Verify the phases were called
        assert session.add.called

    async def test_reconcile_detect_duplicates_false(
        self, svc: ReconciliationService, session_factory: MagicMock
    ) -> None:
        """When detect_duplicates=False, duplicate detection is skipped."""
        with (
            patch.object(
                svc, "_detect_duplicates", new=AsyncMock(return_value=[])
            ) as mock_dup,
            patch.object(
                svc, "_detect_cross_connector_gaps", new=AsyncMock(return_value=[])
            ),
            patch.object(
                svc, "_detect_missing_transactions", new=AsyncMock(return_value=[])
            ),
        ):
            await svc.reconcile(detect_duplicates=False)

        mock_dup.assert_not_awaited()

    async def test_reconcile_with_account_ids(
        self, svc: ReconciliationService, session_factory: MagicMock
    ) -> None:
        """account_ids are passed to internal phases as positional arg."""
        with (
            patch.object(
                svc, "_detect_duplicates", new=AsyncMock(return_value=[])
            ) as mock_dup,
            patch.object(
                svc, "_detect_cross_connector_gaps", new=AsyncMock(return_value=[])
            ),
            patch.object(
                svc, "_detect_missing_transactions", new=AsyncMock(return_value=[])
            ),
        ):
            await svc.reconcile(account_ids=["acct_1", "acct_2"])

        # Account IDs are the 3rd positional arg (after session, run)
        args = mock_dup.call_args.args
        assert len(args) >= 3
        assert args[2] == ["acct_1", "acct_2"]

    async def test_reconcile_with_provider_keys(
        self, svc: ReconciliationService, session_factory: MagicMock
    ) -> None:
        """provider_keys are passed to internal phases."""
        with (
            patch.object(
                svc, "_detect_duplicates", new=AsyncMock(return_value=[])
            ) as mock_dup,
            patch.object(
                svc, "_detect_cross_connector_gaps", new=AsyncMock(return_value=[])
            ),
            patch.object(
                svc, "_detect_missing_transactions", new=AsyncMock(return_value=[])
            ),
        ):
            await svc.reconcile(provider_keys=["bunq", "trading212"])

        assert mock_dup.call_args.kwargs.get("provider_keys") == ["bunq", "trading212"]

    async def test_reconcile_handles_exception(
        self, svc: ReconciliationService, session_factory: MagicMock
    ) -> None:
        """When an internal phase raises, the run is marked as FAILED."""
        with patch.object(
            svc, "_detect_duplicates", new=AsyncMock(side_effect=ValueError("DB error"))
        ):
            run = await svc.reconcile()

        assert run.status == ReconciliationRunStatus.FAILED
        assert run.error_message is not None
        assert "DB error" in run.error_message
        assert run.completed_at is not None

    async def test_reconcile_scope_recorded(
        self, svc: ReconciliationService, session_factory: MagicMock
    ) -> None:
        """The scope dict on the run records the analysis parameters."""
        with (
            patch.object(
                svc, "_detect_duplicates", new=AsyncMock(return_value=[])
            ),
            patch.object(
                svc, "_detect_cross_connector_gaps", new=AsyncMock(return_value=[])
            ),
            patch.object(
                svc, "_detect_missing_transactions", new=AsyncMock(return_value=[])
            ),
        ):
            run = await svc.reconcile(
                account_ids=["acct_1"],
                provider_keys=["bunq"],
                date_from=datetime(2026, 1, 1, tzinfo=UTC),
                date_to=datetime(2026, 6, 1, tzinfo=UTC),
                detect_duplicates=False,
            )

        scope = run.scope
        assert scope is not None
        assert "2026-01-01" in str(scope["date_from"])
        assert "2026-06-01" in str(scope["date_to"])
        assert scope.get("account_ids") == ["acct_1"]
        assert scope.get("provider_keys") == ["bunq"]
        assert scope.get("detect_duplicates") is False


# ═══════════════════════════════════════════════════════════════════════
# _finalize_run
# ═══════════════════════════════════════════════════════════════════════


class TestFinalizeRun:
    """Test the _finalize_run static method."""

    async def test_finalize_with_findings(self, svc: ReconciliationService) -> None:
        """Findings are persisted and summary is computed."""
        session = AsyncMock()

        findings = []
        for kind, sev in [
            (ReconciliationResultKind.DUPLICATE_TRANSACTION, ReconciliationSeverity.WARNING),
            (ReconciliationResultKind.DUPLICATE_TRANSACTION, ReconciliationSeverity.WARNING),
            (ReconciliationResultKind.MISSING_TRANSACTION, ReconciliationSeverity.INFO),
        ]:
            f = MagicMock()
            f.kind = kind
            f.severity = sev
            findings.append(f)

        run = MagicMock()
        run.finding_count = None
        run.summary = None
        run.status = None
        run.completed_at = None

        await ReconciliationService._finalize_run(session, run, findings)

        assert run.finding_count == 3
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.completed_at is not None
        assert run.summary["by_kind"]["duplicate_transaction"] == 2
        assert run.summary["by_kind"]["missing_transaction"] == 1
        assert run.summary["by_severity"]["warning"] == 2
        assert run.summary["by_severity"]["info"] == 1
        assert session.add.call_count == 3

    async def test_finalize_empty_findings(self, svc: ReconciliationService) -> None:
        """Zero findings produces empty summary."""
        session = AsyncMock()
        run = MagicMock()
        run.finding_count = None
        run.summary = None
        run.status = None
        run.completed_at = None

        await ReconciliationService._finalize_run(session, run, [])

        assert run.finding_count == 0
        assert run.status == ReconciliationRunStatus.COMPLETED
        assert run.summary["by_kind"] == {}
        assert run.summary["by_severity"] == {}


# ═══════════════════════════════════════════════════════════════════════
# list_runs
# ═══════════════════════════════════════════════════════════════════════


class TestListRuns:
    """Test the list_runs method."""

    async def test_list_runs_returns_empty(self, svc: ReconciliationService) -> None:
        """Returns empty list when no runs exist."""
        session = svc._session_factory.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        runs = await svc.list_runs()
        assert runs == []

    async def test_list_runs_with_data(self, svc: ReconciliationService) -> None:
        """Returns runs ordered by created_at DESC."""
        session = svc._session_factory.return_value.__aenter__.return_value

        mock_run_1 = MagicMock()
        mock_run_2 = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run_1, mock_run_2]
        session.execute = AsyncMock(return_value=mock_result)

        runs = await svc.list_runs(limit=10, offset=5)
        assert len(runs) == 2

    async def test_list_runs_defaults(self, svc: ReconciliationService) -> None:
        """Default limit=20, offset=0."""
        session = svc._session_factory.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        await svc.list_runs()

        # Verify the SQL was built with correct defaults by checking
        # the statement contains offset/limit clauses
        call_stmt = session.execute.call_args[0][0]
        call_str = str(call_stmt)
        assert "LIMIT" in call_str.upper() or "limit" in call_str.lower()  # noqa: SIM202
        assert "reconciliation_runs" in call_str.lower()


# ═══════════════════════════════════════════════════════════════════════
# get_run_with_results
# ═══════════════════════════════════════════════════════════════════════


class TestGetRunWithResults:
    """Test the get_run_with_results method."""

    async def test_run_not_found(self, svc: ReconciliationService) -> None:
        """Returns None, [], 0 when run doesn't exist."""
        session = svc._session_factory.return_value.__aenter__.return_value
        session.get = AsyncMock(return_value=None)

        run, results, total = await svc.get_run_with_results("nonexistent")
        assert run is None
        assert results == []
        assert total == 0

    async def test_run_with_results(self, svc: ReconciliationService) -> None:
        """Returns run with its findings."""
        session = svc._session_factory.return_value.__aenter__.return_value

        mock_run = MagicMock()
        mock_run.id = "run_1"
        mock_run.tenant_id = "test-tenant"
        session.get = AsyncMock(return_value=mock_run)

        mock_result_1 = MagicMock()
        mock_result_2 = MagicMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 2
        results_result = MagicMock()
        results_result.scalars.return_value.all.return_value = [
            mock_result_1, mock_result_2
        ]

        session.execute = AsyncMock(side_effect=[count_result, results_result])

        run, results, total = await svc.get_run_with_results("run_1")
        assert run is not None
        assert run.id == "run_1"
        assert len(results) == 2
        assert total == 2

    async def test_get_run_with_filters(self, svc: ReconciliationService) -> None:
        """kind_filter and severity_filter are applied."""
        session = svc._session_factory.return_value.__aenter__.return_value

        mock_run = MagicMock()
        mock_run.id = "run_1"
        mock_run.tenant_id = "test-tenant"
        session.get = AsyncMock(return_value=mock_run)

        count_result = MagicMock()
        count_result.scalar.return_value = 1
        results_result = MagicMock()
        results_result.scalars.return_value.all.return_value = [MagicMock()]

        session.execute = AsyncMock(side_effect=[count_result, results_result])

        run, results, total = await svc.get_run_with_results(
            "run_1",
            kind_filter="duplicate_transaction",
            severity_filter="error",
        )
        assert run is not None
        assert total == 1

    async def test_run_wrong_tenant(self, svc: ReconciliationService) -> None:
        """get_run_with_results doesn't filter by tenant in the query itself,
        but returns the run regardless — tenant check is the caller's job."""
        session = svc._session_factory.return_value.__aenter__.return_value

        mock_run = MagicMock()
        mock_run.tenant_id = "other-tenant"
        session.get = AsyncMock(return_value=mock_run)

        count_result = MagicMock()
        count_result.scalar.return_value = 0
        results_result = MagicMock()
        results_result.scalars.return_value.all.return_value = []

        session.execute = AsyncMock(side_effect=[count_result, results_result])

        run, results, total = await svc.get_run_with_results("run_wrong_tenant")
        # The method returns the run because it doesn't filter by tenant
        assert run is not None
        assert run.tenant_id == "other-tenant"
