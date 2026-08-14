"""Feature-flag tests for the exporters (roadmap dr.3 / gap G-13).

Covers the API surface (``/api/v1/exporters``) and the CLI gates for the
Actual Budget and Wealthfolio exporters with the flags on and off.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from finance_sync.api.deps.auth import (
    APIKeyAuthResult,
    AuthContext,
    get_auth_context,
)
from finance_sync.app import create_app
from finance_sync.cli import main
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import Response

_TEST_SECRET: SecretStr = SecretStr("test-secret-key-at-least-16-chars")


def _make_settings(**overrides: Any) -> Settings:
    """Build test settings (no DB/Redis) with optional overrides."""
    defaults: dict[str, Any] = {
        "database_url": None,
        "redis_url": None,
        "secret_key": _TEST_SECRET,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _override_auth(app: FastAPI) -> None:
    """Let requests through as an authenticated API key."""
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        api_key_result=APIKeyAuthResult(tenant_id="tenant-1")
    )


def _mock_db_session() -> AsyncMock:
    """Session whose queries return empty result sets."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar.return_value = 0
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def app() -> FastAPI:
    """App with both exporters enabled (defaults)."""
    return create_app(settings=_make_settings())


@pytest.fixture
def app_ab_disabled() -> FastAPI:
    return create_app(
        settings=_make_settings(exporter_actual_budget_enabled=False)
    )


@pytest.fixture
def app_wf_disabled() -> FastAPI:
    return create_app(
        settings=_make_settings(exporter_wealthfolio_enabled=False)
    )


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_ab_disabled(
    app_ab_disabled: FastAPI,
) -> Generator[TestClient, None, None]:
    with TestClient(app_ab_disabled) as c:
        yield c


@pytest.fixture
def client_wf_disabled(
    app_wf_disabled: FastAPI,
) -> Generator[TestClient, None, None]:
    with TestClient(app_wf_disabled) as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════
# GET /exporters/types — discovery list is filtered by the flags
# ═══════════════════════════════════════════════════════════════════════


class TestTypesEndpoint:
    """``GET /exporters/types`` lists only enabled exporters."""

    def test_types_lists_both_by_default(self, client: TestClient) -> None:
        resp: Response = client.get("/api/v1/exporters/types")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()]
        assert names == ["wealthfolio", "actual-budget"]

    def test_types_omits_actual_budget_when_disabled(
        self, client_ab_disabled: TestClient
    ) -> None:
        resp: Response = client_ab_disabled.get("/api/v1/exporters/types")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()]
        assert names == ["wealthfolio"]

    def test_types_omits_wealthfolio_when_disabled(
        self, client_wf_disabled: TestClient
    ) -> None:
        resp: Response = client_wf_disabled.get("/api/v1/exporters/types")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()]
        assert names == ["actual-budget"]

    def test_types_empty_when_all_exporters_disabled(self) -> None:
        """Both flags off → no exporter types advertised."""
        app_both = create_app(
            settings=_make_settings(
                exporter_actual_budget_enabled=False,
                exporter_wealthfolio_enabled=False,
            )
        )
        with TestClient(app_both) as c:
            resp: Response = c.get("/api/v1/exporters/types")
            assert resp.status_code == 200
            assert resp.json() == []


# ═══════════════════════════════════════════════════════════════════════
# GET /exporters/config — Wealthfolio config endpoint
# ═══════════════════════════════════════════════════════════════════════


class TestConfigEndpoint:
    """``GET /exporters/config`` is gated on the Wealthfolio flag."""

    def test_config_disabled_returns_404(
        self, client_wf_disabled: TestClient
    ) -> None:
        """Flag dependency runs before auth — 404 even without credentials."""
        resp: Response = client_wf_disabled.get("/api/v1/exporters/config")
        assert resp.status_code == 404
        assert "Wealthfolio exporter is disabled" in resp.json()["detail"]

    def test_config_available_when_only_ab_disabled(
        self, app_ab_disabled: FastAPI, client_ab_disabled: TestClient
    ) -> None:
        """Disabling Actual Budget does not hide the Wealthfolio surface."""
        _override_auth(app_ab_disabled)
        resp: Response = client_ab_disabled.get("/api/v1/exporters/config")
        assert resp.status_code == 200
        assert resp.json()["exporter_type"] == "wealthfolio"

    def test_config_enabled_returns_config(
        self, app: FastAPI, client: TestClient
    ) -> None:
        _override_auth(app)
        resp: Response = client.get("/api/v1/exporters/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exporter_type"] == "wealthfolio"
        assert data["output_dir"] == "/tmp/finance_sync_wealthfolio_exports"
        assert data["default_currency"] == "EUR"


# ═══════════════════════════════════════════════════════════════════════
# POST /exporters/export — Wealthfolio export trigger
# ═══════════════════════════════════════════════════════════════════════


class TestExportEndpoint:
    """``POST /exporters/export`` is gated on the Wealthfolio flag."""

    def test_export_disabled_returns_404(
        self, client_wf_disabled: TestClient
    ) -> None:
        """Flag dependency runs before auth — 404 even without credentials."""
        resp: Response = client_wf_disabled.post("/api/v1/exporters/export")
        assert resp.status_code == 404
        assert "Wealthfolio exporter is disabled" in resp.json()["detail"]

    def test_export_requires_auth_when_enabled(
        self, app_ab_disabled: FastAPI, client_ab_disabled: TestClient
    ) -> None:
        """AB off does not gate WF export — request proceeds to auth (401)."""
        # Auth resolution touches the DB session, so stub it out.
        app_ab_disabled.dependency_overrides[get_db] = lambda: (
            _mock_db_session()
        )
        resp: Response = client_ab_disabled.post("/api/v1/exporters/export")
        assert resp.status_code == 401

    @patch("finance_sync.api.v1.exporters.WealthfolioExporter")
    @patch("finance_sync.api.v1.exporters.get_container")
    def test_export_enabled_returns_202(
        self,
        mock_get_container: MagicMock,
        mock_exporter_cls: MagicMock,
        app: FastAPI,
        client: TestClient,
    ) -> None:
        """With the flag on, the export trigger runs and returns 202."""
        _override_auth(app)

        fake_settings = SimpleNamespace(
            exporter_wealthfolio_enabled=True,
            wealthfolio_output_dir="/tmp/out",
            wealthfolio_default_currency="EUR",
            wealthfolio_export_holdings=True,
            wealthfolio_max_transactions_per_file=10_000,
            wealthfolio_include_pending=False,
            wealthfolio_account_name_overrides={},
            wealthfolio_instrument_type_overrides={},
        )
        fake_container = MagicMock()
        fake_container.settings = fake_settings
        fake_container.session_factory = MagicMock()
        mock_get_container.return_value = fake_container

        result = MagicMock()
        result.status = "completed"
        result.accounts_mapped = 2
        result.transactions_attempted = 5
        result.transactions_exported = 5
        result.transactions_failed = 0
        result.transactions_skipped = 0
        result.holdings_exported = 3
        result.csv_files = ["/tmp/out/transactions.csv"]
        result.duration_s = 1.5
        result.error_message = None
        mock_exporter_cls.return_value.run_export = AsyncMock(
            return_value=result
        )

        resp: Response = client.post("/api/v1/exporters/export")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "completed"
        assert data["transactions_exported"] == 5
        assert data["csv_files"] == ["/tmp/out/transactions.csv"]


# ═══════════════════════════════════════════════════════════════════════
# GET /exporters/runs — audit history is not gated
# ═══════════════════════════════════════════════════════════════════════


class TestRunsEndpoint:
    """Run history stays readable regardless of the flags."""

    def test_runs_history_available_when_wf_disabled(
        self, app_wf_disabled: FastAPI, client_wf_disabled: TestClient
    ) -> None:
        _override_auth(app_wf_disabled)
        app_wf_disabled.dependency_overrides[get_db] = lambda: (
            _mock_db_session()
        )
        resp: Response = client_wf_disabled.get("/api/v1/exporters/runs")
        assert resp.status_code == 200
        assert resp.json() == {"runs": [], "total": 0}


# ═══════════════════════════════════════════════════════════════════════
# CLI gates — exporter commands refuse to run when the flag is off
# ═══════════════════════════════════════════════════════════════════════


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
