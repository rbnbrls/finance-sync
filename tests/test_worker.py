"""Tests for the worker module — monitoring, retry, and scheduler setup."""
# pyright: basic

from __future__ import annotations

import asyncio

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
            worker_job_trading212_sync_enabled=False,
            worker_job_price_enrichment_enabled=False,
            worker_job_reconciliation_enabled=False,
            worker_job_outbox_enabled=False,
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
