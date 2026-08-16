"""Tests for the worker module — monitoring, retry, and scheduler setup."""
# pyright: basic

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.worker.jobs import JobRetryError, retry_with_backoff
from finance_sync.worker.monitoring import (
    JobHistory,
    JobMonitor,
    JobRunContext,
    JobRunResult,
)

# ── Monitoring tests ────────────────────────────────────────────────────


class TestJobRunResult:
    """JobRunResult dataclass behaviour."""

    def test_success_result(self) -> None:
        result = JobRunResult(
            job_id="test_job",
            job_name="Test Job",
            started_at=100.0,
            duration_s=1.5,
            success=True,
        )
        assert result.job_id == "test_job"
        assert result.success is True
        assert result.error is None

    def test_failure_result(self) -> None:
        result = JobRunResult(
            job_id="test_job",
            job_name="Test Job",
            started_at=100.0,
            duration_s=0.5,
            success=False,
            error="Something broke",
        )
        assert result.success is False
        assert result.error == "Something broke"


class TestJobHistory:
    """JobHistory rolling window and statistics."""

    def test_initial_state(self) -> None:
        history = JobHistory()
        assert history.total_runs == 0
        assert history.total_failures == 0
        assert history.last_run is None
        assert history.success_rate == 1.0

    def test_record_success(self) -> None:
        history = JobHistory()
        result = JobRunResult(
            job_id="j1",
            job_name="Job 1",
            started_at=0.0,
            duration_s=1.0,
            success=True,
        )
        history.record(result)

        assert history.total_runs == 1
        assert history.total_failures == 0
        assert history.last_run is result
        assert history.last_error is None
        assert history.last_duration_s == 1.0
        assert history.success_rate == 1.0

    def test_record_failure(self) -> None:
        history = JobHistory()
        result = JobRunResult(
            job_id="j1",
            job_name="Job 1",
            started_at=0.0,
            duration_s=0.5,
            success=False,
            error="fail",
        )
        history.record(result)

        assert history.total_runs == 1
        assert history.total_failures == 1
        assert history.last_error is result
        assert history.success_rate == 0.0

    def test_rolling_window(self) -> None:
        """Only the last 20 runs are kept."""
        history = JobHistory()
        for i in range(25):
            history.record(
                JobRunResult(
                    job_id="j1",
                    job_name="Job 1",
                    started_at=float(i),
                    duration_s=0.1,
                    success=True,
                ),
            )

        assert history.total_runs == 25
        assert len(history.recent_runs) == 20
        # The first entry in recent_runs should be run index 5 (0-based: 5..24)
        assert history.recent_runs[0].started_at == 5.0

    def test_success_rate_edge_cases(self) -> None:
        history = JobHistory()
        # No runs
        assert history.success_rate == 1.0

        # All failures
        for _ in range(3):
            history.record(
                JobRunResult(
                    job_id="j1",
                    job_name="Job 1",
                    started_at=0.0,
                    duration_s=0.1,
                    success=False,
                    error="err",
                ),
            )
        assert history.success_rate == 0.0

        # Mixed
        for _ in range(2):
            history.record(
                JobRunResult(
                    job_id="j1",
                    job_name="Job 1",
                    started_at=0.0,
                    duration_s=0.1,
                    success=True,
                ),
            )
        # 2 successes out of 5 total
        assert history.success_rate == 0.4


class TestJobMonitor:
    """JobMonitor — aggregate tracking across multiple jobs."""

    def test_record_and_get(self) -> None:
        monitor = JobMonitor()
        result = JobRunResult(
            job_id="sync_bunq",
            job_name="Sync Bunq",
            started_at=0.0,
            duration_s=2.0,
            success=True,
        )
        monitor.record(result)

        history = monitor.get_history("sync_bunq")
        assert history is not None
        assert history.total_runs == 1
        assert history.last_run is result

        # Unknown job returns None
        assert monitor.get_history("nonexistent") is None

    def test_all_jobs(self) -> None:
        monitor = JobMonitor()
        monitor.record(
            JobRunResult("a", "A", 0.0, 1.0, True),
        )
        monitor.record(
            JobRunResult("b", "B", 0.0, 2.0, False, error="fail"),
        )

        all_jobs = monitor.all_jobs()
        assert set(all_jobs) == {"a", "b"}
        assert all_jobs["a"].total_runs == 1
        assert all_jobs["b"].total_runs == 1

    def test_summarize(self) -> None:
        monitor = JobMonitor()
        monitor.record(
            JobRunResult("a", "A", 0.0, 1.0, True),
        )
        monitor.record(
            JobRunResult("b", "B", 0.0, 2.0, False, error="fail"),
        )

        summary = monitor.summarize()
        assert len(summary) == 2
        job_ids = {s["job_id"] for s in summary}
        assert job_ids == {"a", "b"}

        failed_job = next(s for s in summary if s["job_id"] == "b")
        assert failed_job["last_run_success"] is False
        assert failed_job["last_error"] == "fail"

    def test_record_updates_prometheus_gauges(self) -> None:
        """Record() publishes duration + success rate gauges (G-06)."""
        from prometheus_client import REGISTRY

        monitor = JobMonitor()
        monitor.record(
            JobRunResult("gauge_job", "Gauge Job", 0.0, 3.5, True),
        )

        assert (
            REGISTRY.get_sample_value(
                "worker_job_duration_seconds",
                {"job_id": "gauge_job"},
            )
            == 3.5
        )
        assert (
            REGISTRY.get_sample_value(
                "worker_job_success_rate",
                {"job_id": "gauge_job"},
            )
            == 1.0
        )

        # A failure drops the success rate below 1
        monitor.record(
            JobRunResult("gauge_job", "Gauge Job", 0.0, 1.0, False),
        )
        assert (
            REGISTRY.get_sample_value(
                "worker_job_success_rate",
                {"job_id": "gauge_job"},
            )
            == 0.5
        )

    async def test_worker_health_metrics_route(self) -> None:
        """Worker health server exposes /metrics (G-06 worker scrape)."""
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from finance_sync.worker.health import WorkerHealthServer

        server = WorkerHealthServer(port=0)
        app = web.Application()
        app.router.add_get("/metrics", server._handle_metrics)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert "outbox_messages_pending_total" in body
            assert "worker_job_duration_seconds" in body


class TestJobRunContext:
    """JobRunContext — async context manager for job monitoring."""

    @pytest.mark.asyncio
    async def test_success_path(self) -> None:
        monitor = JobMonitor()
        async with JobRunContext(monitor, "test", name="Test") as ctx:
            ctx.set_details({"processed": 10})

        history = monitor.get_history("test")
        assert history is not None
        assert history.total_runs == 1
        assert history.last_run is not None
        assert history.last_run.success is True
        assert history.last_run.details == {"processed": 10}

    @pytest.mark.asyncio
    async def test_failure_path(self) -> None:
        monitor = JobMonitor()
        with pytest.raises(ValueError, match="boom"):
            async with JobRunContext(monitor, "failing", name="Failing"):
                msg = "boom"
                raise ValueError(msg)

        history = monitor.get_history("failing")
        assert history is not None
        assert history.total_runs == 1
        assert history.last_run is not None
        assert history.last_run.success is False
        assert history.last_run.error is not None


# ── Retry tests ─────────────────────────────────────────────────────────


class TestRetryWithBackoff:
    """Exponential backoff retry behaviour."""

    @pytest.mark.asyncio
    async def test_success_first_attempt(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            return "done"

        result = await retry_with_backoff(
            factory,
            max_attempts=3,
            base_delay=0.01,
            job_name="test",
        )
        assert result == "done"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "transient error"
                raise ConnectionError(msg)
            return "done"

        result = await retry_with_backoff(
            factory,
            max_attempts=3,
            base_delay=0.01,
            job_name="test",
        )
        assert result == "done"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhaust_retries(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            msg = "always fails"
            raise ValueError(msg)

        with pytest.raises(JobRetryError) as exc_info:
            await retry_with_backoff(
                factory,
                max_attempts=3,
                base_delay=0.01,
                job_name="test",
            )

        assert call_count == 3
        assert "test" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_single_attempt(self) -> None:
        """max_attempts=1 means no retry."""
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            msg = "fail"
            raise RuntimeError(msg)

        with pytest.raises(JobRetryError):
            await retry_with_backoff(
                factory,
                max_attempts=1,
                base_delay=0.01,
                job_name="test",
            )

        assert call_count == 1


# ── Settings tests ──────────────────────────────────────────────────────


class TestWorkerSettings:
    """Worker-related settings are loaded correctly."""

    def test_default_values(self) -> None:
        from finance_sync.config.settings import Settings

        settings = Settings()  # type: ignore[call-arg]

        assert settings.worker_enabled is True
        assert settings.worker_health_port == 9090

        assert settings.worker_job_bunq_sync_enabled is True
        assert settings.worker_job_bunq_sync_interval_minutes == 15

        assert settings.worker_job_bunq_cards_enabled is True
        assert settings.worker_job_bunq_cards_interval_hours == 1

        assert settings.worker_job_trading212_sync_enabled is True
        assert settings.worker_job_trading212_sync_interval_hours == 1

        assert settings.worker_job_price_enrichment_enabled is True
        assert settings.worker_job_price_enrichment_interval_minutes == 15

        assert settings.worker_job_reconciliation_enabled is True
        assert settings.worker_job_reconciliation_cron == "0 2 * * *"
        assert settings.worker_job_reconciliation_after_sync_enabled is True

        assert settings.worker_job_outbox_enabled is True
        assert settings.worker_job_outbox_interval_seconds == 30

        assert settings.worker_retry_max_attempts == 3
        assert settings.worker_retry_base_delay_s == 1.0

    def test_export_job_default_off_without_push_target(
        self, monkeypatch
    ) -> None:
        """WORKER_JOB_EXPORT_ENABLED unset → default follows the push env.

        Exact default: enabled only when WEALTHFOLIO_SERVER_URL and
        WEALTHFOLIO_PASSWORD are both set.  Here neither is set, so the
        sweep defaults to disabled.
        """
        from finance_sync.config.settings import Settings

        monkeypatch.delenv("WORKER_JOB_EXPORT_ENABLED", raising=False)
        monkeypatch.delenv("WEALTHFOLIO_SERVER_URL", raising=False)
        monkeypatch.delenv("WEALTHFOLIO_PASSWORD", raising=False)

        settings = Settings()  # type: ignore[call-arg]
        assert settings.worker_job_export_enabled is False
        assert settings.worker_job_export_interval_minutes == 5

    def test_export_job_default_on_when_push_target_configured(
        self, monkeypatch
    ) -> None:
        """Both gating env vars set → sweep defaults to enabled."""
        from finance_sync.config.settings import Settings

        monkeypatch.delenv("WORKER_JOB_EXPORT_ENABLED", raising=False)
        monkeypatch.setenv("WEALTHFOLIO_SERVER_URL", "http://192.168.3.50:8080")
        monkeypatch.setenv("WEALTHFOLIO_PASSWORD", "s3cret")

        settings = Settings()  # type: ignore[call-arg]
        assert settings.worker_job_export_enabled is True

    def test_export_job_explicit_flag_wins(self, monkeypatch) -> None:
        """Explicit WORKER_JOB_EXPORT_ENABLED overrides the derived default."""
        from finance_sync.config.settings import Settings

        # Explicit false beats configured push target.
        monkeypatch.setenv("WORKER_JOB_EXPORT_ENABLED", "false")
        monkeypatch.setenv("WEALTHFOLIO_SERVER_URL", "http://192.168.3.50:8080")
        monkeypatch.setenv("WEALTHFOLIO_PASSWORD", "s3cret")
        settings = Settings()  # type: ignore[call-arg]
        assert settings.worker_job_export_enabled is False

        # Explicit true beats missing push target.
        monkeypatch.setenv("WORKER_JOB_EXPORT_ENABLED", "true")
        monkeypatch.delenv("WEALTHFOLIO_SERVER_URL", raising=False)
        monkeypatch.delenv("WEALTHFOLIO_PASSWORD", raising=False)
        settings = Settings()  # type: ignore[call-arg]
        assert settings.worker_job_export_enabled is True


# ── WorkerScheduler tests ───────────────────────────────────────────────


class TestWorkerScheduler:
    """APScheduler wrapper behaviour."""

    @pytest.mark.asyncio
    async def test_create_and_start_stop(self) -> None:
        """Verify scheduler lifecycle."""
        from finance_sync.config.settings import Settings
        from finance_sync.container import Container
        from finance_sync.worker.monitoring import JobMonitor
        from finance_sync.worker.scheduler import WorkerScheduler

        settings = Settings(  # type: ignore[call-arg]
            database_url=None,  # No DB — use in-memory job store
            worker_job_bunq_sync_enabled=False,
            worker_job_bunq_cards_enabled=False,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
            worker_job_degiro_watch_enabled=False,
        )
        container = Container.from_settings(settings)
        monitor = JobMonitor()

        scheduler = WorkerScheduler(settings, container, monitor)
        assert scheduler.is_running() is False
        assert scheduler.running_jobs() == []

        await scheduler.start()
        assert scheduler.is_running()
        assert scheduler.job_summary() == []  # All jobs disabled

        await scheduler.stop()
        # After stop(), the scheduler's internal loop has exited.
        # We verify it by checking that the scheduler no longer reports
        # running in its APScheduler state.
        await asyncio.sleep(0.05)
        running_after_stop = scheduler.is_running()
        # APScheduler may report running briefly; if so, retry after
        # a short delay.  The important thing is that it stops eventually.
        if running_after_stop:
            await asyncio.sleep(0.1)
            running_after_stop = scheduler.is_running()
        assert running_after_stop is False

    @pytest.mark.asyncio
    async def test_job_summary_with_enabled_jobs(self) -> None:
        """Check that enabled jobs appear in the summary."""
        from finance_sync.config.settings import Settings
        from finance_sync.container import Container
        from finance_sync.worker.monitoring import JobMonitor
        from finance_sync.worker.scheduler import WorkerScheduler

        settings = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_bunq_sync_enabled=True,
            worker_job_bunq_cards_enabled=False,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=True,
        )
        container = Container.from_settings(settings)
        monitor = JobMonitor()

        scheduler = WorkerScheduler(settings, container, monitor)
        await scheduler.start()

        summary = scheduler.job_summary()
        job_ids = {j["id"] for j in summary}
        assert "sync_bunq" in job_ids
        assert "process_outbox" in job_ids
        assert "sync_trading212" not in job_ids
        assert "sync_bunq_cards" not in job_ids

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_bunq_cards_job_flag_gates_registration(self) -> None:
        """The sync_bunq_cards job only registers when its flag is on."""
        from finance_sync.config.settings import Settings
        from finance_sync.container import Container
        from finance_sync.worker.monitoring import JobMonitor
        from finance_sync.worker.scheduler import WorkerScheduler

        # Flag ON → job registered with the configured hourly trigger
        settings_on = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_bunq_sync_enabled=False,
            worker_job_bunq_cards_enabled=True,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
        )
        container = Container.from_settings(settings_on)
        scheduler_on = WorkerScheduler(settings_on, container, JobMonitor())
        await scheduler_on.start()
        job_ids_on = {j["id"] for j in scheduler_on.job_summary()}
        assert "sync_bunq_cards" in job_ids_on
        await scheduler_on.stop()

        # Flag OFF → job not registered
        settings_off = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_bunq_sync_enabled=False,
            worker_job_bunq_cards_enabled=False,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
        )
        container_off = Container.from_settings(settings_off)
        scheduler_off = WorkerScheduler(
            settings_off, container_off, JobMonitor()
        )
        await scheduler_off.start()
        job_ids_off = {j["id"] for j in scheduler_off.job_summary()}
        assert "sync_bunq_cards" not in job_ids_off
        await scheduler_off.stop()

    @pytest.mark.asyncio
    async def test_export_sweep_job_flag_gates_registration(self) -> None:
        """The export_wealthfolio job registers with the 5-min interval
        trigger when enabled, and not at all when disabled."""
        from finance_sync.config.settings import Settings
        from finance_sync.container import Container
        from finance_sync.worker.monitoring import JobMonitor
        from finance_sync.worker.scheduler import WorkerScheduler

        # Flag ON → job registered with the default 5-minute trigger
        settings_on = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_bunq_sync_enabled=False,
            worker_job_bunq_cards_enabled=False,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
            worker_job_export_enabled=True,
        )
        container_on = Container.from_settings(settings_on)
        scheduler_on = WorkerScheduler(settings_on, container_on, JobMonitor())
        await scheduler_on.start()
        jobs = {j["id"]: j for j in scheduler_on.job_summary()}
        assert "export_wealthfolio" in jobs
        assert "interval[0:05:00]" in jobs["export_wealthfolio"]["trigger"]
        await scheduler_on.stop()

        # Flag OFF → job not registered
        settings_off = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_bunq_sync_enabled=False,
            worker_job_bunq_cards_enabled=False,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
            worker_job_export_enabled=False,
        )
        container_off = Container.from_settings(settings_off)
        scheduler_off = WorkerScheduler(
            settings_off, container_off, JobMonitor()
        )
        await scheduler_off.start()
        job_ids_off = {j["id"] for j in scheduler_off.job_summary()}
        assert "export_wealthfolio" not in job_ids_off
        await scheduler_off.stop()

    @pytest.mark.asyncio
    async def test_export_sweep_job_custom_interval(self) -> None:
        """WORKER_JOB_EXPORT_INTERVAL_MINUTES changes the sweep cadence."""
        from finance_sync.config.settings import Settings
        from finance_sync.container import Container
        from finance_sync.worker.monitoring import JobMonitor
        from finance_sync.worker.scheduler import WorkerScheduler

        settings = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_bunq_sync_enabled=False,
            worker_job_bunq_cards_enabled=False,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
            worker_job_export_enabled=True,
            worker_job_export_interval_minutes=15,
        )
        container = Container.from_settings(settings)
        scheduler = WorkerScheduler(settings, container, JobMonitor())
        await scheduler.start()
        jobs = {j["id"]: j for j in scheduler.job_summary()}
        assert "export_wealthfolio" in jobs
        assert "interval[0:15:00]" in jobs["export_wealthfolio"]["trigger"]
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_pause_resume(self) -> None:
        """Pause and resume lifecycle."""
        from finance_sync.config.settings import Settings
        from finance_sync.container import Container
        from finance_sync.worker.monitoring import JobMonitor
        from finance_sync.worker.scheduler import WorkerScheduler

        settings = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_bunq_sync_enabled=False,
            worker_job_bunq_cards_enabled=False,
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
        )
        container = Container.from_settings(settings)
        monitor = JobMonitor()

        scheduler = WorkerScheduler(settings, container, monitor)
        await scheduler.start()
        assert scheduler.is_running()

        scheduler.pause()
        # After pausing, the scheduler still reports running but paused
        # (APScheduler keeps running but doesn't fire triggers)
        still_running = scheduler.is_running()
        assert still_running  # paused != stopped

        scheduler.resume()
        assert scheduler.is_running()

        await scheduler.stop()


# ── export_wealthfolio_job tests ───────────────────────────────────────


class TestExportWealthfolioJob:
    """Wealthfolio delivery sweep — gating, per-tenant push, cursor resume."""

    @staticmethod
    def _make_container(
        *,
        enabled: bool = True,
        server_url: str = "http://192.168.3.50:8080",
        password: str = "s3cret",
        tenant_ids: list[str] | None = None,
    ) -> tuple[MagicMock, MagicMock]:
        """Build a container whose session exposes the given tenants."""
        from types import SimpleNamespace

        from finance_sync.config.settings import Settings

        settings = Settings(  # type: ignore[call-arg]
            database_url=None,
            worker_job_export_enabled=enabled,
            worker_job_export_interval_minutes=5,
            wealthfolio_server_url=server_url,
            wealthfolio_password=password,
        )

        tenants = [
            SimpleNamespace(id=tid) for tid in (tenant_ids or ["tenant-1"])
        ]

        session = MagicMock()
        session.info = {}
        uow = MagicMock()
        uow.tenants.list = AsyncMock(return_value=tenants)

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)

        container = MagicMock()
        container.settings = settings
        container.session_factory.return_value = cm
        return container, uow

    @pytest.mark.asyncio
    async def test_skips_cleanly_when_disabled(self) -> None:
        """Flag off → log + skip, no client/exporter is ever built."""
        from finance_sync.worker.jobs import export_wealthfolio_job

        container, _uow = self._make_container(enabled=False)
        with patch(
            "finance_sync.exporter.wealthfolio.client.WealthfolioClient"
        ) as mock_client:
            result = await export_wealthfolio_job(container)

        assert result["status"] == "skipped"
        assert "WORKER_JOB_EXPORT_ENABLED" in result["reason"]
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_cleanly_when_target_unconfigured(self) -> None:
        """Push env vars missing → log + skip, no crash."""
        from finance_sync.worker.jobs import export_wealthfolio_job

        container, _uow = self._make_container(
            enabled=True,
            server_url="",
            password="",
        )
        with patch(
            "finance_sync.exporter.wealthfolio.client.WealthfolioClient"
        ) as mock_client:
            result = await export_wealthfolio_job(container)

        assert result["status"] == "skipped"
        assert "WEALTHFOLIO_SERVER_URL" in result["reason"]
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_pushes_all_tenants(self) -> None:
        """Configured → authenticates once and pushes per tenant."""
        from finance_sync.worker.jobs import export_wealthfolio_job

        container, uow = self._make_container(
            tenant_ids=["tenant-1", "tenant-2"]
        )

        with (
            patch(
                "finance_sync.exporter.wealthfolio.client.WealthfolioClient"
            ) as mock_client_cls,
            patch(
                "finance_sync.exporter.wealthfolio.exporter.WealthfolioExporter"
            ) as mock_exporter_cls,
            patch("finance_sync.worker.jobs.UnitOfWork", return_value=uow),
        ):
            client = mock_client_cls.return_value
            client.authenticate = AsyncMock(return_value=True)
            client.close = AsyncMock(return_value=None)

            exporter = mock_exporter_cls.return_value
            exporter.push_to_wealthfolio = AsyncMock(
                return_value={
                    "imported": 3,
                    "skipped": 0,
                    "failed": 0,
                    "run_id": "run-1",
                },
            )

            result = await export_wealthfolio_job(container)

        assert result["status"] == "completed"
        assert result["tenants"] == 2
        assert result["failed"] == 0
        assert [r["tenant_id"] for r in result["results"]] == [
            "tenant-1",
            "tenant-2",
        ]
        assert all(r["status"] == "completed" for r in result["results"])
        # One shared authenticated client for the whole sweep
        client.authenticate.assert_awaited_once()
        client.close.assert_awaited_once()
        assert exporter.push_to_wealthfolio.await_count == 2

    @pytest.mark.asyncio
    async def test_tenant_failure_does_not_abort_sweep(self) -> None:
        """A failing tenant is recorded; the remaining tenants still push."""
        from finance_sync.worker.jobs import export_wealthfolio_job

        container, uow = self._make_container(
            tenant_ids=["tenant-1", "tenant-2"]
        )

        with (
            patch(
                "finance_sync.exporter.wealthfolio.client.WealthfolioClient"
            ) as mock_client_cls,
            patch(
                "finance_sync.exporter.wealthfolio.exporter.WealthfolioExporter"
            ) as mock_exporter_cls,
            patch("finance_sync.worker.jobs.UnitOfWork", return_value=uow),
        ):
            client = mock_client_cls.return_value
            client.authenticate = AsyncMock(return_value=True)
            client.close = AsyncMock(return_value=None)

            exporter = mock_exporter_cls.return_value
            exporter.push_to_wealthfolio = AsyncMock(
                side_effect=[
                    RuntimeError("Wealthfolio rejected batch"),
                    {
                        "imported": 1,
                        "skipped": 0,
                        "failed": 0,
                        "run_id": "run-2",
                    },
                ],
            )

            result = await export_wealthfolio_job(container)

        assert result["status"] == "completed"
        assert result["failed"] == 1
        statuses = {r["tenant_id"]: r["status"] for r in result["results"]}
        assert statuses == {
            "tenant-1": "failed",
            "tenant-2": "completed",
        }
        failed = next(
            r for r in result["results"] if r["tenant_id"] == "tenant-1"
        )
        assert "rejected" in failed["error"]

    @pytest.mark.asyncio
    async def test_cursor_makes_sweep_idempotent_across_runs(self) -> None:
        """A second sweep resumes from the delivery cursor (G-14).

        ``push_to_wealthfolio`` is cursor-driven: it only pushes
        transactions newer than the last delivered ``(occurred_at, id)``
        per account.  Simulating the exporter returning 0 imported on the
        second run (cursor already at the latest transaction) must not
        re-push anything — the sweep's own result reflects that.
        """
        from finance_sync.worker.jobs import export_wealthfolio_job

        container, uow = self._make_container(tenant_ids=["tenant-1"])

        push_calls = 0

        async def fake_push(wf_client: object, **kwargs: object) -> dict:
            nonlocal push_calls
            push_calls += 1
            if push_calls == 1:
                return {
                    "imported": 5,
                    "skipped": 0,
                    "failed": 0,
                    "run_id": "run-1",
                }
            # Cursor advanced past all transactions → nothing to push.
            return {
                "imported": 0,
                "skipped": 0,
                "failed": 0,
                "run_id": "run-2",
            }

        with (
            patch(
                "finance_sync.exporter.wealthfolio.client.WealthfolioClient"
            ) as mock_client_cls,
            patch(
                "finance_sync.exporter.wealthfolio.exporter.WealthfolioExporter"
            ) as mock_exporter_cls,
            patch("finance_sync.worker.jobs.UnitOfWork", return_value=uow),
        ):
            client = mock_client_cls.return_value
            client.authenticate = AsyncMock(return_value=True)
            client.close = AsyncMock(return_value=None)

            exporter = mock_exporter_cls.return_value
            exporter.push_to_wealthfolio = AsyncMock(side_effect=fake_push)

            first = await export_wealthfolio_job(container)
            second = await export_wealthfolio_job(container)

        # Sweep delegates to the cursor-driven push (no since override),
        # so a warm cursor yields a no-op push instead of duplicates.
        assert first["results"][0]["imported"] == 5
        assert second["results"][0]["imported"] == 0
        assert push_calls == 2
        assert exporter.push_to_wealthfolio.await_count == 2
        # Every call resumes from the cursor: no since/accounts overrides.
        for call in exporter.push_to_wealthfolio.await_args_list:
            assert call.kwargs == {"wf_client": client}
