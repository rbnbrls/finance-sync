"""Exporter CLI gates (roadmap dr.3 / gap G-13).

The HTTP exporter surface is retired (see ``test_exporter_retirement.py``);
these tests keep covering the still-compatible legacy CLI commands, which
remain a backwards-compatible deployment path but reject a run when the
corresponding global exporter flag is disabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from finance_sync.cli import main

_TEST_SECRET: SecretStr = SecretStr("test-secret-key-at-least-16-chars")


class TestCliGates:
    """``finance-sync wealthfolio|actual-budget ...`` exit 2 when disabled."""

    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_wealthfolio_cli_disabled_exits_2(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_settings_cls.return_value = SimpleNamespace(
            is_production=False,
            log_level="DEBUG",
            exporter_wealthfolio_enabled=False,
        )

        with pytest.raises(SystemExit) as exc:
            main(["wealthfolio", "export"])
        assert exc.value.code == 2

        captured = capsys.readouterr()
        assert "disabled" in captured.err
        assert "EXPORTER_WEALTHFOLIO_ENABLED" in captured.err
        mock_from_settings.assert_not_called()

    @patch("finance_sync.cli.Container.from_settings")
    @patch("finance_sync.cli.Settings")
    def test_actual_budget_cli_disabled_exits_2(
        self,
        mock_settings_cls: MagicMock,
        mock_from_settings: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_settings_cls.return_value = SimpleNamespace(
            is_production=False,
            log_level="DEBUG",
            exporter_actual_budget_enabled=False,
        )

        with pytest.raises(SystemExit) as exc:
            main(["actual-budget", "export"])
        assert exc.value.code == 2

        captured = capsys.readouterr()
        assert "disabled" in captured.err
        assert "EXPORTER_ACTUAL_BUDGET_ENABLED" in captured.err
        mock_from_settings.assert_not_called()
