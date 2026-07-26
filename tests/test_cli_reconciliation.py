"""Tests for the reconciliation CLI commands.

Tests argument parsing and handler logic with mocked dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.cli import _build_parser, main

# ═══════════════════════════════════════════════════════════════════════
# Argument parsing tests
# ═══════════════════════════════════════════════════════════════════════


class TestReconcileParser:
    """Verify the ``reconcile`` subcommand argument parsing."""

    def test_defaults(self) -> None:
        """Default values for optional flags."""
        parser = _build_parser()
        args = parser.parse_args(["reconcile"])

        assert args.command == "reconcile"
        assert args.account_ids is None
        assert args.provider_keys is None
        assert args.date_from is None
        assert args.date_to is None
        assert args.days_back == 90
        assert args.threshold_hours == 48
        assert args.tenant_id is None

    def test_account_ids(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["reconcile", "--account-ids", "acct_1,acct_2"]
        )
        assert args.account_ids == "acct_1,acct_2"

    def test_provider_keys(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["reconcile", "--provider-keys", "bunq,trading212"]
        )
        assert args.provider_keys == "bunq,trading212"

    def test_date_from(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["reconcile", "--date-from", "2026-01-01"])
        assert args.date_from == "2026-01-01"
        assert args.days_back == 90  # unchanged when --date-from is set

    def test_date_to(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["reconcile", "--date-to", "2026-06-30T23:59:59Z"]
        )
        assert args.date_to == "2026-06-30T23:59:59Z"

    def test_date_range(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "reconcile",
                "--date-from",
                "2026-01-01",
                "--date-to",
                "2026-06-30",
            ]
        )
        assert args.date_from == "2026-01-01"
        assert args.date_to == "2026-06-30"

    def test_all_options(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "reconcile",
                "--account-ids",
                "acct_1,acct_2",
                "--provider-keys",
                "bunq,trading212",
                "--date-from",
                "2026-01-01",
                "--date-to",
                "2026-06-30",
                "--days-back",
                "30",
                "--threshold-hours",
                "24",
                "--tenant-id",
                "tenant_xyz",
            ]
        )
        assert args.command == "reconcile"
        assert args.account_ids == "acct_1,acct_2"
        assert args.provider_keys == "bunq,trading212"
        assert args.date_from == "2026-01-01"
        assert args.date_to == "2026-06-30"
        assert args.days_back == 30
        assert args.threshold_hours == 24
        assert args.tenant_id == "tenant_xyz"

    def test_tenant_id(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["reconcile", "--tenant-id", "tenant_xyz"])
        assert args.tenant_id == "tenant_xyz"


class TestCompareParser:
    """Verify the ``compare`` subcommand argument parsing."""

    def test_required_positional_args(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["compare", "bunq", "trading212"])
        assert args.command == "compare"
        assert args.connector_a == "bunq"
        assert args.connector_b == "trading212"
        assert args.date_from is None
        assert args.date_to is None
        assert args.threshold_hours == 48
        assert args.tenant_id is None

    def test_date_range(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "compare",
                "bunq",
                "trading212",
                "--date-from",
                "2026-01-01",
                "--date-to",
                "2026-06-30",
                "--threshold-hours",
                "24",
                "--tenant-id",
                "tenant_xyz",
            ]
        )
        assert args.connector_a == "bunq"
        assert args.connector_b == "trading212"
        assert args.date_from == "2026-01-01"
        assert args.date_to == "2026-06-30"
        assert args.threshold_hours == 24
        assert args.tenant_id == "tenant_xyz"

    def test_help_output(self, capsys: pytest.CaptureFixture) -> None:
        """Verify help text contains key option descriptions."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["compare", "--help"])
        captured = capsys.readouterr()
        help_text = captured.out
        assert "connector_a" in help_text
        assert "connector_b" in help_text
        assert "First connector/provider key" in help_text
        assert "Second connector/provider key" in help_text


# ═══════════════════════════════════════════════════════════════════════
# main() dispatch tests
# ═══════════════════════════════════════════════════════════════════════


class TestMainDispatch:
    """Verify main() routes commands to correct handlers."""

    def test_unknown_command(self) -> None:
        """Unknown subcommand exits with 2."""
        with pytest.raises(SystemExit) as exc:
            main(["unknown-cmd"])
        assert exc.value.code == 2

    def test_help_requested(self) -> None:
        """No args with subparsers required exits 2."""
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 2

    def test_reconcile_help(self, capsys: pytest.CaptureFixture) -> None:
        """``reconcile --help`` prints help and exits 0."""
        with pytest.raises(SystemExit) as exc:
            main(["reconcile", "--help"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "--account-ids" in captured.out
        assert "--provider-keys" in captured.out
        assert "--date-from" in captured.out
        assert "--date-to" in captured.out
        assert "--days-back" in captured.out
        assert "--threshold-hours" in captured.out
        assert "--tenant-id" in captured.out

    def test_compare_help(self, capsys: pytest.CaptureFixture) -> None:
        """``compare --help`` prints help and exits 0."""
        with pytest.raises(SystemExit) as exc:
            main(["compare", "--help"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "connector_a" in captured.out
        assert "connector_b" in captured.out


# ═══════════════════════════════════════════════════════════════════════
# _cmd_reconcile handler tests (mocked DB)
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_run_completed() -> MagicMock:
    """Mock ReconciliationRun with completed status and no findings."""
    run = MagicMock()
    run.id = "run-123"
    run.tenant_id = "test-tenant"
    run.status = "completed"
    run.finding_count = 0
    run.summary = {
        "by_kind": {},
        "by_severity": {},
    }
    run.error_message = None
    run.started_at = datetime.now(UTC) - timedelta(seconds=10)
    run.completed_at = datetime.now(UTC)
    return run


@pytest.fixture
def mock_run_with_findings() -> MagicMock:
    """Mock ReconciliationRun with some findings."""
    run = MagicMock()
    run.id = "run-456"
    run.tenant_id = "test-tenant"
    run.status = "completed"
    run.finding_count = 3
    run.summary = {
        "by_kind": {
            "duplicate_transaction": 2,
            "missing_transaction": 1,
        },
        "by_severity": {
            "warning": 2,
            "info": 1,
        },
    }
    run.error_message = None
    run.started_at = datetime.now(UTC) - timedelta(seconds=10)
    run.completed_at = datetime.now(UTC)
    return run


@pytest.fixture
def mock_run_failed() -> MagicMock:
    """Mock ReconciliationRun with failed status."""
    run = MagicMock()
    run.id = "run-789"
    run.tenant_id = "test-tenant"
    run.status = "failed"
    run.finding_count = 0
    run.summary = {"by_kind": {}, "by_severity": {}}
    run.error_message = "Something went wrong"
    run.started_at = datetime.now(UTC) - timedelta(seconds=10)
    run.completed_at = datetime.now(UTC)
    return run


@pytest.fixture
def mock_tenants() -> list[MagicMock]:
    """Mock list of tenants."""
    t1 = MagicMock()
    t1.id = "tenant-1"
    return [t1]


# ═══════════════════════════════════════════════════════════════════════
# _cmd_reconcile handler tests (mocked DB)
# ═══════════════════════════════════════════════════════════════════════


class TestCmdReconcile:
    """Test the ``reconcile`` command handler."""

    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_no_tenants_exits_2(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
    ) -> None:
        """Exit code 2 when no tenants are found."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(return_value=[])

        with (
            patch(
                "finance_sync.cli.UnitOfWork",
                return_value=mock_uow_instance,
            ),
            pytest.raises(SystemExit) as exc,
        ):
            main(["reconcile"])
        assert exc.value.code == 2

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_success_no_findings(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        capsys: pytest.CaptureFixture,
        mock_run_completed: MagicMock,
    ) -> None:
        """Exit 0 when reconciliation completes with no findings."""
        # Mock Settings
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        # Mock container
        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        # Mock UoW
        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        # Mock session for tenant lookup
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        # Mock ReconciliationService
        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_completed)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(["reconcile", "--tenant-id", "tenant-1"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "No findings" in captured.out
        assert "look consistent" in captured.out

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_success_with_findings(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        capsys: pytest.CaptureFixture,
        mock_run_with_findings: MagicMock,
    ) -> None:
        """Exit 1 when reconciliation finds discrepancies."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_with_findings)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(["reconcile", "--tenant-id", "tenant-1"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "3 finding(s)" in captured.out
        assert "duplicate_transaction" in captured.out
        assert "missing_transaction" in captured.out

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_handler_fails_gracefully(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        mock_run_failed: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 2 when reconciliation run fails."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_failed)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(["reconcile", "--tenant-id", "tenant-1"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "FAILED" in captured.out or "failed" in captured.out

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_handler_exception_exits_2(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 2 when reconcile raises an unexpected exception."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(side_effect=ValueError("DB error"))
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(["reconcile", "--tenant-id", "tenant-1"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "FAILED" in captured.out


# ═══════════════════════════════════════════════════════════════════════
# _cmd_compare handler tests (mocked DB)
# ═══════════════════════════════════════════════════════════════════════


class TestCmdCompare:
    """Test the ``compare`` command handler."""

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_success_no_findings(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        capsys: pytest.CaptureFixture,
        mock_run_completed: MagicMock,
    ) -> None:
        """Exit 0 when connectors match with no findings."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_completed)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(["compare", "bunq", "trading212", "--tenant-id", "tenant-1"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "No findings" in captured.out
        assert "connectors look consistent" in captured.out

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_success_with_findings(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        capsys: pytest.CaptureFixture,
        mock_run_with_findings: MagicMock,
    ) -> None:
        """Exit 1 when comparison finds discrepancies."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_with_findings)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(["compare", "bunq", "trading212", "--tenant-id", "tenant-1"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "finding(s)" in captured.out
        assert "bunq" in captured.out or "trading212" in captured.out


# ═══════════════════════════════════════════════════════════════════════
# Reconcile parser — new options tests
# ═══════════════════════════════════════════════════════════════════════


class TestReconcileParserNewOptions:
    """Verify new connector-a/b and detect-duplicates args."""

    def test_defaults(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["reconcile"])
        assert args.connector_a is None
        assert args.connector_b is None
        assert args.detect_duplicates is True

    def test_connector_a_and_b(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "reconcile",
                "--connector-a",
                "bunq",
                "--connector-b",
                "trading212",
            ]
        )
        assert args.connector_a == "bunq"
        assert args.connector_b == "trading212"

    def test_no_detect_duplicates(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["reconcile", "--no-detect-duplicates"])
        assert args.detect_duplicates is False

    def test_detect_duplicates_explicit(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["reconcile", "--detect-duplicates"])
        assert args.detect_duplicates is True

    def test_connector_help_contains_options(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """Help text mentions the new options."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["reconcile", "--help"])
        captured = capsys.readouterr()
        assert "--connector-a" in captured.out
        assert "--connector-b" in captured.out
        assert "--detect-duplicates" in captured.out
        assert "--no-detect-duplicates" in captured.out


# ═══════════════════════════════════════════════════════════════════════
# _cmd_reconcile handler — connector comparison
# ═══════════════════════════════════════════════════════════════════════


class TestCmdReconcileConnectorComparison:
    """Test the ``reconcile`` command with --connector-a/--connector-b."""

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_connector_a_b_passed_as_provider_keys(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        mock_run_completed: MagicMock,
    ) -> None:
        """--connector-a/--connector-b are used as provider_keys."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_completed)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "reconcile",
                    "--tenant-id",
                    "tenant-1",
                    "--connector-a",
                    "bunq",
                    "--connector-b",
                    "trading212",
                ]
            )
        assert exc.value.code == 0

        # Verify provider_keys were passed to the service
        call_kwargs = mock_svc.reconcile.call_args.kwargs
        assert call_kwargs.get("provider_keys") == ["bunq", "trading212"]

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_connector_a_without_b_exits_2(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
    ) -> None:
        """Only --connector-a without --connector-b exits with 2."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "reconcile",
                    "--tenant-id",
                    "tenant-1",
                    "--connector-a",
                    "bunq",
                ]
            )
        assert exc.value.code == 2

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_same_connector_a_b_exits_2(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
    ) -> None:
        """Same value for --connector-a and --connector-b exits with 2."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "reconcile",
                    "--tenant-id",
                    "tenant-1",
                    "--connector-a",
                    "bunq",
                    "--connector-b",
                    "bunq",
                ]
            )
        assert exc.value.code == 2

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_no_detect_duplicates_passed_to_service(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        mock_run_completed: MagicMock,
    ) -> None:
        """--no-detect-duplicates is passed to the service."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_completed)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "reconcile",
                    "--tenant-id",
                    "tenant-1",
                    "--no-detect-duplicates",
                ]
            )
        assert exc.value.code == 0

        call_kwargs = mock_svc.reconcile.call_args.kwargs
        assert call_kwargs.get("detect_duplicates") is False


# ═══════════════════════════════════════════════════════════════════════
# Compare parser — new --detect-duplicates option
# ═══════════════════════════════════════════════════════════════════════


class TestCompareParserNewOptions:
    """Verify new ``--detect-duplicates`` option on the compare subcommand."""

    def test_detect_duplicates_default(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["compare", "bunq", "trading212"])
        assert args.detect_duplicates is True

    def test_no_detect_duplicates(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["compare", "bunq", "trading212", "--no-detect-duplicates"]
        )
        assert args.detect_duplicates is False

    def test_help_mentions_detect_duplicates(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["compare", "--help"])
        captured = capsys.readouterr()
        assert "--detect-duplicates" in captured.out

    @patch("finance_sync.cli.ReconciliationService")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_no_detect_duplicates_passed_to_service(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_svc_cls: MagicMock,
        mock_run_completed: MagicMock,
    ) -> None:
        """--no-detect-duplicates on compare is passed to the service."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.is_production = False
        mock_settings_obj.log_level = "DEBUG"
        mock_settings_cls.return_value = mock_settings_obj

        mock_container = MagicMock()
        mock_session_factory = MagicMock()
        mock_container.session_factory = mock_session_factory
        mock_from_settings.return_value = mock_container
        mock_container.dispose.return_value.__aenter__.return_value = None
        mock_container.dispose.return_value.__aexit__.return_value = None

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(
            return_value=[MagicMock(id="tenant-1")]
        )
        mock_uow_cls.return_value = mock_uow_instance

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        mock_session_factory.return_value.__aexit__.return_value = None

        mock_svc = AsyncMock()
        mock_svc.reconcile = AsyncMock(return_value=mock_run_completed)
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "compare",
                    "bunq",
                    "trading212",
                    "--tenant-id",
                    "tenant-1",
                    "--no-detect-duplicates",
                ]
            )
        assert exc.value.code == 0

        call_kwargs = mock_svc.reconcile.call_args.kwargs
        assert call_kwargs.get("detect_duplicates") is False
