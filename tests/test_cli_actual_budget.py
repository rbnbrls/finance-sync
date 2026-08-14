"""Tests for the Actual Budget CLI commands.

Covers argument parsing, main() dispatch and handler logic with mocked
dependencies (mirrors the Wealthfolio CLI test pattern in
``test_cli_reconciliation.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.cli import _build_parser, main

# ═══════════════════════════════════════════════════════════════════════
# Argument parsing tests
# ═══════════════════════════════════════════════════════════════════════


class TestActualBudgetParser:
    """Verify the ``actual-budget`` subcommand argument parsing."""

    def test_export_defaults(self) -> None:
        """Default values for the export subcommand."""
        parser = _build_parser()
        args = parser.parse_args(["actual-budget", "export"])

        assert args.command == "actual-budget"
        assert args.ab_command == "export"
        assert args.output_dir is None
        assert args.account_ids is None
        assert args.days_back == 90
        assert args.max_transactions is None

    def test_export_all_options(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "actual-budget",
                "export",
                "--output-dir",
                "/tmp/ab_out",
                "--account-ids",
                "acct_1,acct_2",
                "--days-back",
                "30",
                "--max-transactions",
                "25",
            ]
        )
        assert args.output_dir == "/tmp/ab_out"
        assert args.account_ids == "acct_1,acct_2"
        assert args.days_back == 30
        assert args.max_transactions == 25

    def test_push_defaults(self) -> None:
        """Default values for the push subcommand."""
        parser = _build_parser()
        args = parser.parse_args(["actual-budget", "push"])

        assert args.command == "actual-budget"
        assert args.ab_command == "push"
        assert args.server_url is None
        assert args.password is None
        assert args.account_ids is None
        assert args.days_back == 90
        assert args.max_transactions is None
        assert args.dry_run is False

    def test_push_all_options(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "actual-budget",
                "push",
                "--server-url",
                "http://localhost:5006",
                "--password",
                "hunter2",
                "--account-ids",
                "acct_1",
                "--days-back",
                "7",
                "--max-transactions",
                "10",
                "--dry-run",
            ]
        )
        assert args.server_url == "http://localhost:5006"
        assert args.password == "hunter2"
        assert args.account_ids == "acct_1"
        assert args.days_back == 7
        assert args.max_transactions == 10
        assert args.dry_run is True

    def test_export_help(self, capsys: pytest.CaptureFixture) -> None:
        """``actual-budget export --help`` lists the expected flags."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["actual-budget", "export", "--help"])
        captured = capsys.readouterr()
        assert "--output-dir" in captured.out
        assert "--account-ids" in captured.out
        assert "--days-back" in captured.out
        assert "--max-transactions" in captured.out

    def test_push_help(self, capsys: pytest.CaptureFixture) -> None:
        """``actual-budget push --help`` lists the expected flags."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["actual-budget", "push", "--help"])
        captured = capsys.readouterr()
        assert "--server-url" in captured.out
        assert "--password" in captured.out
        assert "--dry-run" in captured.out

    def test_missing_subcommand_exits_2(self) -> None:
        """``actual-budget`` without a subcommand exits 2."""
        with pytest.raises(SystemExit) as exc:
            main(["actual-budget"])
        assert exc.value.code == 2


# ═══════════════════════════════════════════════════════════════════════
# Shared handler mocks
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_container() -> MagicMock:
    """Mock Container with a working session factory and settings."""
    mock_settings = SimpleNamespace(
        is_production=False,
        log_level="DEBUG",
        actual_budget_server_url="http://localhost:5006",
        actual_budget_password="test-password",
    )

    mock_container = MagicMock()
    mock_container.settings = mock_settings
    mock_container.dispose.return_value.__aenter__.return_value = None
    mock_container.dispose.return_value.__aexit__.return_value = None

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mock_session_factory = MagicMock(return_value=AsyncMock())
    mock_session_factory.return_value.__aenter__.return_value = mock_session
    mock_session_factory.return_value.__aexit__.return_value = None
    mock_container.session_factory = mock_session_factory
    return mock_container


@pytest.fixture
def mock_tenant() -> MagicMock:
    """Mock UoW returning a single tenant."""
    mock_uow_instance = MagicMock()
    mock_uow_instance.tenants = MagicMock()
    mock_uow_instance.tenants.list = AsyncMock(
        return_value=[MagicMock(id="tenant-1")]
    )
    return mock_uow_instance


def _mock_result(**overrides: object) -> MagicMock:
    """Build a mock ExportResult-like object."""
    result = MagicMock()
    result.status = "completed"
    result.accounts_mapped = 1
    result.transactions_attempted = 3
    result.transactions_exported = 3
    result.transactions_failed = 0
    result.error_message = None
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


# ═══════════════════════════════════════════════════════════════════════
# Handler tests
# ═══════════════════════════════════════════════════════════════════════


class TestCmdActualBudgetExport:
    """Test the ``actual-budget export`` command handler."""

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_export_success(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        mock_tenant: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 0 when the export cycle completes."""
        mock_settings_cls.return_value = mock_container.settings
        mock_from_settings.return_value = mock_container
        mock_uow_cls.return_value = mock_tenant

        mock_exporter = mock_exporter_cls.return_value
        mock_exporter.run_export = AsyncMock(
            return_value=_mock_result(accounts_mapped=2)
        )

        with pytest.raises(SystemExit) as exc:
            main(["actual-budget", "export"])
        assert exc.value.code == 0

        captured = capsys.readouterr()
        assert "Actual Budget export starting" in captured.out
        assert "Result: completed" in captured.out
        assert "3/3" in captured.out
        assert "Accounts:     2" in captured.out
        # CSV written into default output dir
        assert "/tmp/finance_sync_ab_exports" in captured.out

        # Verify exporter was wired with the resolved tenant + config
        _, kwargs = mock_exporter_cls.call_args
        assert kwargs["tenant_id"] == "tenant-1"
        assert kwargs["ab_config"].server_url == "http://localhost:5006"

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_export_failure(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        mock_tenant: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 2 when the export cycle fails."""
        mock_settings_cls.return_value = mock_container.settings
        mock_from_settings.return_value = mock_container
        mock_uow_cls.return_value = mock_tenant

        mock_exporter = mock_exporter_cls.return_value
        mock_exporter.run_export = AsyncMock(
            return_value=_mock_result(
                status="failed", error_message="Connection refused"
            )
        )

        with pytest.raises(SystemExit) as exc:
            main(["actual-budget", "export"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "Result: failed" in captured.out
        assert "Connection refused" in captured.out + captured.err

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_export_no_tenants(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 2 when no tenants exist."""
        mock_settings_cls.return_value = mock_container.settings
        mock_from_settings.return_value = mock_container

        mock_uow_instance = MagicMock()
        mock_uow_instance.tenants = MagicMock()
        mock_uow_instance.tenants.list = AsyncMock(return_value=[])
        mock_uow_cls.return_value = mock_uow_instance

        with pytest.raises(SystemExit) as exc:
            main(["actual-budget", "export"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "No tenants found" in captured.out + captured.err
        mock_exporter_cls.assert_not_called()


class TestCmdActualBudgetPush:
    """Test the ``actual-budget push`` command handler."""

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_push_success(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        mock_tenant: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 0 when the push completes."""
        mock_settings_cls.return_value = mock_container.settings
        mock_from_settings.return_value = mock_container
        mock_uow_cls.return_value = mock_tenant

        mock_exporter = mock_exporter_cls.return_value
        mock_exporter.run_export = AsyncMock(
            return_value=_mock_result(transactions_exported=5)
        )

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "actual-budget",
                    "push",
                    "--server-url",
                    "http://localhost:5006",
                    "--password",
                    "hunter2",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "Actual Budget push starting" in captured.out
        assert "Status:       completed" in captured.out
        assert "5/3" in captured.out

        # Exporter must be configured with the CLI-provided credentials
        _, kwargs = mock_exporter_cls.call_args
        assert kwargs["ab_config"].server_url == "http://localhost:5006"
        assert kwargs["ab_config"].password == "hunter2"

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_push_missing_server_url(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        mock_tenant: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 2 when no server URL is configured."""
        mock_settings_cls.return_value = SimpleNamespace(
            is_production=False,
            log_level="DEBUG",
            actual_budget_server_url="",
            actual_budget_password="test-password",
        )
        mock_container.settings = mock_settings_cls.return_value
        mock_from_settings.return_value = mock_container
        mock_uow_cls.return_value = mock_tenant

        with pytest.raises(SystemExit) as exc:
            main(["actual-budget", "push"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "server URL not configured" in captured.out + captured.err
        mock_exporter_cls.assert_not_called()

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_push_missing_password(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        mock_tenant: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 2 when no password is configured."""
        mock_settings_cls.return_value = SimpleNamespace(
            is_production=False,
            log_level="DEBUG",
            actual_budget_server_url="http://localhost:5006",
            actual_budget_password="",
        )
        mock_container.settings = mock_settings_cls.return_value
        mock_from_settings.return_value = mock_container
        mock_uow_cls.return_value = mock_tenant

        with pytest.raises(SystemExit) as exc:
            main(["actual-budget", "push"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "password not configured" in captured.out + captured.err
        mock_exporter_cls.assert_not_called()

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_push_dry_run(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        mock_tenant: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 0 with a count when --dry-run is passed."""
        mock_settings_cls.return_value = mock_container.settings
        mock_from_settings.return_value = mock_container
        mock_uow_cls.return_value = mock_tenant

        acct = MagicMock()
        acct.id = "acct-1"
        acct.name = "Checking"
        txn = MagicMock()
        txn.id = "txn-1"

        mock_exporter = mock_exporter_cls.return_value
        mock_exporter._load_accounts = AsyncMock(return_value=[acct])
        mock_exporter._fetch_pending_transactions = AsyncMock(
            return_value=[txn, txn]
        )

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "actual-budget",
                    "push",
                    "--dry-run",
                    "--server-url",
                    "http://localhost:5006",
                    "--password",
                    "hunter2",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "[Checking] 2 pending transactions" in captured.out
        assert "Dry run: 2 transaction(s) ready to push." in captured.out
        # Dry run must not call run_export
        mock_exporter.run_export.assert_not_called()

    @patch("finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter")
    @patch("finance_sync.cli.UnitOfWork")
    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_push_failure(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        mock_uow_cls: MagicMock,
        mock_exporter_cls: MagicMock,
        mock_container: MagicMock,
        mock_tenant: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Exit 2 when the push fails."""
        mock_settings_cls.return_value = mock_container.settings
        mock_from_settings.return_value = mock_container
        mock_uow_cls.return_value = mock_tenant

        mock_exporter = mock_exporter_cls.return_value
        mock_exporter.run_export = AsyncMock(
            return_value=_mock_result(
                status="failed", error_message="Auth failed"
            )
        )

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "actual-budget",
                    "push",
                    "--server-url",
                    "http://localhost:5006",
                    "--password",
                    "hunter2",
                ]
            )
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "Status:       failed" in captured.out
        assert "Auth failed" in captured.out + captured.err
