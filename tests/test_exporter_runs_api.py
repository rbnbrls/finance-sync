"""API tests for export-run DLQ visibility and retry (Gap G-14).

Covers:
- ``GET /exporters/runs`` with ``?status=error`` (DLQ) filtering and
  error-message / exporter_type fields in the responses.
- ``POST /exporters/{type}/runs/{id}/retry`` — retries failed runs,
  and the guard rails around it (404 unknown run/type, 409 non-failed
  or exporter-type mismatch, 404 when the exporter flag is off).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finance_sync.api.deps.auth import (
    APIKeyAuthResult,
    AuthContext,
    get_auth_context,
)
from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db
from finance_sync.exporter.models import ExportRun

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

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


def _now() -> datetime:
    return datetime.now(UTC)


def _make_run(
    *,
    status: str = "failed",
    exporter_type: str | None = "wealthfolio",
    error_message: str | None = None,
    started_at: datetime | None = None,
) -> ExportRun:
    """Build an ExportRun row (not yet persisted)."""
    return ExportRun(
        exporter_type=exporter_type,
        status=status,
        started_at=started_at or (_now() - timedelta(hours=1)),
        completed_at=_now() if status != "running" else None,
        transactions_attempted=10,
        transactions_exported=5,
        transactions_failed=5 if status == "failed" else 0,
        error_message=error_message,
    )


# ═══════════════════════════════════════════════════════════════════════
# Fixtures — real aiosqlite session for the runs/retry endpoints
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db_session_factory() -> Generator[
    async_sessionmaker[AsyncSession], None, None
]:
    """In-memory SQLite session factory with the export_runs table."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(ExportRun.__table__.create)

    asyncio.run(_setup())
    yield factory
    asyncio.run(engine.dispose())


def _seed_runs(
    factory: async_sessionmaker[AsyncSession],
    runs: list[ExportRun],
) -> None:
    """Persist ExportRun rows and return their ids."""

    async def _seed() -> None:
        async with factory() as session:
            for run in runs:
                session.add(run)
            await session.commit()

    asyncio.run(_seed())


@pytest.fixture
def app(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    app = create_app(settings=_make_settings())
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        api_key_result=APIKeyAuthResult(tenant_id="tenant-1")
    )

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    return app


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def _fake_container(
    *,
    wealthfolio_enabled: bool = True,
    actual_budget_enabled: bool = True,
) -> MagicMock:
    """Container stub with exporter flag settings for the retry endpoint."""
    fake_settings = SimpleNamespace(
        exporter_wealthfolio_enabled=wealthfolio_enabled,
        exporter_actual_budget_enabled=actual_budget_enabled,
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
    return fake_container


def _mock_export_outcome(**overrides: Any) -> MagicMock:
    """Result object returned by a mocked exporter run."""
    outcome = MagicMock()
    outcome.status = "completed"
    outcome.transactions_attempted = 3
    outcome.transactions_exported = 3
    outcome.transactions_failed = 0
    outcome.error_message = None
    outcome.duration_s = 0.75
    # Real exporter results carry the new run's id; tests that exercise
    # the retry response set it explicitly.
    outcome.run_id = None
    for k, v in overrides.items():
        setattr(outcome, k, v)
    return outcome


# ═══════════════════════════════════════════════════════════════════════
# GET /exporters/runs — status filtering (DLQ) + error fields
# ═══════════════════════════════════════════════════════════════════════


class TestListRunsStatusFilter:
    def test_status_error_lists_only_failed_runs_with_errors(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        _seed_runs(
            db_session_factory,
            [
                _make_run(status="failed", error_message="Connection refused"),
                _make_run(status="completed"),
                _make_run(
                    status="failed",
                    error_message="1 account(s) failed to push: Broker: boom",
                    exporter_type="wealthfolio",
                ),
            ],
        )

        resp: Response = client.get("/api/v1/exporters/runs?status=error")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert all(r["status"] == "failed" for r in data["runs"])
        assert {r["error_message"] for r in data["runs"]} == {
            "Connection refused",
            "1 account(s) failed to push: Broker: boom",
        }
        assert all(r["exporter_type"] == "wealthfolio" for r in data["runs"])
        # error detail is present on every listed DLQ run
        assert all(r["error_message"] for r in data["runs"])

    def test_status_failed_alias_matches_error(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        _seed_runs(
            db_session_factory,
            [_make_run(status="failed", error_message="boom")],
        )
        resp_error = client.get("/api/v1/exporters/runs?status=error")
        resp_failed = client.get("/api/v1/exporters/runs?status=failed")
        assert resp_error.status_code == 200
        assert resp_failed.status_code == 200
        assert resp_error.json()["total"] == resp_failed.json()["total"] == 1

    def test_status_completed_filters_out_failed(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        _seed_runs(
            db_session_factory,
            [
                _make_run(status="failed", error_message="boom"),
                _make_run(status="completed", exporter_type="actual-budget"),
            ],
        )
        resp: Response = client.get("/api/v1/exporters/runs?status=completed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["runs"][0]["status"] == "completed"
        assert data["runs"][0]["exporter_type"] == "actual-budget"

    def test_no_filter_returns_all_runs(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        _seed_runs(
            db_session_factory,
            [
                _make_run(status="failed"),
                _make_run(status="completed"),
                _make_run(status="running"),
            ],
        )
        resp: Response = client.get("/api/v1/exporters/runs")
        assert resp.status_code == 200
        assert resp.json()["total"] == 3


# ═══════════════════════════════════════════════════════════════════════
# POST /exporters/{type}/runs/{id}/retry
# ═══════════════════════════════════════════════════════════════════════


class TestRetryExportRun:
    def test_retry_failed_wealthfolio_run(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        failed_run = _make_run(status="failed", error_message="disk full")
        _seed_runs(db_session_factory, [failed_run])

        # The retried run is a NEW run created by the exporter — the
        # response must report it from the result, not the failed run.
        retried_run_id = str(uuid4())
        outcome = _mock_export_outcome(run_id=retried_run_id)

        fake_container = _fake_container()
        with (
            patch(
                "finance_sync.api.v1.exporters.get_container",
                return_value=fake_container,
            ),
            patch("finance_sync.api.v1.exporters.WealthfolioExporter") as m_cls,
        ):
            m_cls.return_value.run_export = AsyncMock(return_value=outcome)
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{failed_run.id}/retry"
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["run_id"] == retried_run_id
        assert data["run_id"] != str(failed_run.id)
        assert data["status"] == "completed"
        assert data["transactions_exported"] == 3
        assert data["error_message"] is None
        m_cls.return_value.run_export.assert_awaited_once()

    def test_retry_failed_actual_budget_run(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        failed_run = _make_run(
            status="failed",
            exporter_type="actual-budget",
            error_message="ab down",
        )
        _seed_runs(db_session_factory, [failed_run])

        retried_run_id = str(uuid4())
        outcome = _mock_export_outcome(run_id=retried_run_id)

        fake_container = _fake_container()
        with (
            patch(
                "finance_sync.api.v1.exporters.get_container",
                return_value=fake_container,
            ),
            patch(
                "finance_sync.api.v1.exporters.ActualBudgetExporter"
            ) as m_cls,
        ):
            m_cls.return_value.run_export = AsyncMock(return_value=outcome)
            resp: Response = client.post(
                f"/api/v1/exporters/actual-budget/runs/{failed_run.id}/retry"
            )

        assert resp.status_code == 202
        assert resp.json()["status"] == "completed"
        assert resp.json()["run_id"] == retried_run_id
        m_cls.return_value.run_export.assert_awaited_once()

    def test_retry_run_id_comes_from_result_not_newest_run(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A newer unrelated run must not shadow the retried run's id.

        Regression for the review finding that the response used a
        global ``ORDER BY started_at DESC LIMIT 1`` lookup, which picks
        the wrong run under concurrency (worker sweep, parallel
        retries, other tenants).
        """
        failed_run = _make_run(status="failed", error_message="boom")
        newer_unrelated = _make_run(status="completed")
        _seed_runs(db_session_factory, [failed_run, newer_unrelated])

        retried_run_id = str(uuid4())
        outcome = _mock_export_outcome(run_id=retried_run_id)

        fake_container = _fake_container()
        with (
            patch(
                "finance_sync.api.v1.exporters.get_container",
                return_value=fake_container,
            ),
            patch("finance_sync.api.v1.exporters.WealthfolioExporter") as m_cls,
        ):
            m_cls.return_value.run_export = AsyncMock(return_value=outcome)
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{failed_run.id}/retry"
            )

        assert resp.status_code == 202
        assert resp.json()["run_id"] == retried_run_id
        assert resp.json()["run_id"] != str(newer_unrelated.id)

    def test_retry_falls_back_to_newest_run_when_result_has_no_run_id(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Defensive fallback when the exporter result lacks a run id."""
        failed_run = _make_run(status="failed", error_message="boom")
        _seed_runs(db_session_factory, [failed_run])

        fake_container = _fake_container()
        with (
            patch(
                "finance_sync.api.v1.exporters.get_container",
                return_value=fake_container,
            ),
            patch("finance_sync.api.v1.exporters.WealthfolioExporter") as m_cls,
        ):
            m_cls.return_value.run_export = AsyncMock(
                return_value=_mock_export_outcome(run_id=None)
            )
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{failed_run.id}/retry"
            )

        assert resp.status_code == 202
        assert resp.json()["run_id"] == str(failed_run.id)

    def test_retry_completed_run_conflict(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        completed = _make_run(status="completed")
        _seed_runs(db_session_factory, [completed])

        with patch(
            "finance_sync.api.v1.exporters.get_container",
            return_value=_fake_container(),
        ):
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{completed.id}/retry"
            )

        assert resp.status_code == 409
        assert "Only failed export runs can be retried" in resp.json()["detail"]

    def test_retry_missing_run_404(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        with patch(
            "finance_sync.api.v1.exporters.get_container",
            return_value=_fake_container(),
        ):
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{uuid4()}/retry"
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Export run not found"

    def test_retry_unknown_exporter_type_404(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        failed_run = _make_run(status="failed")
        _seed_runs(db_session_factory, [failed_run])

        with patch(
            "finance_sync.api.v1.exporters.get_container",
            return_value=_fake_container(),
        ):
            resp: Response = client.post(
                f"/api/v1/exporters/bogus/runs/{failed_run.id}/retry"
            )
        assert resp.status_code == 404
        assert "Unknown exporter type" in resp.json()["detail"]

    def test_retry_exporter_type_mismatch_409(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ab_run = _make_run(
            status="failed", exporter_type="actual-budget", error_message="x"
        )
        _seed_runs(db_session_factory, [ab_run])

        with patch(
            "finance_sync.api.v1.exporters.get_container",
            return_value=_fake_container(),
        ):
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{ab_run.id}/retry"
            )
        assert resp.status_code == 409
        assert "belongs to exporter" in resp.json()["detail"]

    def test_retry_disabled_exporter_404(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        failed_run = _make_run(status="failed")
        _seed_runs(db_session_factory, [failed_run])

        with patch(
            "finance_sync.api.v1.exporters.get_container",
            return_value=_fake_container(wealthfolio_enabled=False),
        ):
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{failed_run.id}/retry"
            )
        assert resp.status_code == 404
        assert "Wealthfolio exporter is disabled" in resp.json()["detail"]

    def test_retry_legacy_run_without_exporter_type_allowed(
        self,
        client: TestClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pre-0012 runs have exporter_type NULL — retry still works."""
        legacy_run = _make_run(
            status="failed", exporter_type=None, error_message="old failure"
        )
        _seed_runs(db_session_factory, [legacy_run])

        fake_container = _fake_container()
        with (
            patch(
                "finance_sync.api.v1.exporters.get_container",
                return_value=fake_container,
            ),
            patch("finance_sync.api.v1.exporters.WealthfolioExporter") as m_cls,
        ):
            m_cls.return_value.run_export = AsyncMock(
                return_value=_mock_export_outcome()
            )
            resp: Response = client.post(
                f"/api/v1/exporters/wealthfolio/runs/{legacy_run.id}/retry"
            )
        assert resp.status_code == 202
        assert resp.json()["status"] == "completed"
