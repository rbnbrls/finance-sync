"""Job monitoring — track duration, success/failure, and errors per job run.

The ``JobMonitor`` is an in-memory store that records the outcome of every
scheduled job execution.  Results are accessible to the health endpoint and
could be persisted to the database in a later phase.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class JobRunResult:
    """Outcome of a single job execution."""

    job_id: str
    job_name: str
    started_at: float  # monotonic timestamp
    duration_s: float
    success: bool
    error: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class JobHistory:
    """Rolling history of runs for a single job."""

    total_runs: int = 0
    total_failures: int = 0
    last_run: JobRunResult | None = None
    last_error: JobRunResult | None = None
    recent_runs: list[JobRunResult] = field(default_factory=list)

    @property
    def last_duration_s(self) -> float | None:
        return self.last_run.duration_s if self.last_run else None

    @property
    def success_rate(self) -> float:
        if self.total_runs == 0:
            return 1.0
        return (self.total_runs - self.total_failures) / self.total_runs

    def record(self, result: JobRunResult) -> None:
        """Record a run result."""
        self.total_runs += 1
        self.last_run = result
        if not result.success:
            self.total_failures += 1
            self.last_error = result
        # Keep last 20 runs in the rolling window
        self.recent_runs.append(result)
        if len(self.recent_runs) > 20:
            self.recent_runs.pop(0)


class JobRunContext:
    """Async context manager that wraps a job execution with monitoring.

    Usage::

        async with JobRunContext(monitor, job_id, name="my-job") as ctx:
            result = await do_work()
            ctx.set_details({"processed": result})
    """

    def __init__(
        self,
        monitor: JobMonitor,
        job_id: str,
        *,
        name: str = "",
    ) -> None:
        self._monitor = monitor
        self._job_id = job_id
        self._job_name = name or job_id
        self._start: float = 0.0
        self._details: dict[str, Any] | None = None

    def set_details(self, details: dict[str, Any]) -> None:
        self._details = details

    async def __aenter__(self) -> JobRunContext:
        self._start = time.monotonic()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        duration = time.monotonic() - self._start
        result = JobRunResult(
            job_id=self._job_id,
            job_name=self._job_name,
            started_at=self._start,
            duration_s=round(duration, 4),
            success=exc_type is None,
            error=str(exc_val) if exc_val else None,
            details=self._details,
        )
        self._monitor.record(result)
        # Don't suppress exceptions — let the caller handle them
        return False


class JobMonitor:
    """In-memory job run monitor.

    Records and exposes run history for every scheduled job.  Thread-safe
    only when accessed from the same event loop (APScheduler uses the same
    loop as the worker, so this is fine).
    """

    def __init__(self) -> None:
        self._history: dict[str, JobHistory] = {}

    def record(self, result: JobRunResult) -> None:
        """Record a job run result."""
        if result.job_id not in self._history:
            self._history[result.job_id] = JobHistory()
        self._history[result.job_id].record(result)

    def get_history(self, job_id: str) -> JobHistory | None:
        """Return run history for a specific job."""
        return self._history.get(job_id)

    def all_jobs(self) -> dict[str, JobHistory]:
        """Return history for all tracked jobs."""
        return dict(self._history)

    def summarize(self) -> list[dict[str, Any]]:
        """Return a compact summary of all jobs for the health endpoint."""
        summary: list[dict[str, Any]] = []
        for job_id, history in self._history.items():
            entry: dict[str, Any] = {
                "job_id": job_id,
                "total_runs": history.total_runs,
                "total_failures": history.total_failures,
                "success_rate": round(history.success_rate, 4),
            }
            if history.last_run:
                entry["last_run_duration_s"] = history.last_run.duration_s
                entry["last_run_success"] = history.last_run.success
                entry["last_error"] = history.last_run.error
            summary.append(entry)
        return summary


# -- Decorator-style convenience helper ----------------------------------


def monitored_job(
    monitor: JobMonitor,
    job_id: str,
    *,
    name: str = "",
) -> Callable[..., Any]:
    """Decorator that wraps an async job function with monitoring.

    Usage::

        @monitored_job(monitor, "my_job")
        async def my_job(some_arg: str) -> int:
            ...
    """

    def decorator(
        func: Callable[..., Any],
    ) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with JobRunContext(
                monitor,
                job_id,
                name=name or job_id,
            ) as ctx:
                result = await func(*args, **kwargs)
                if isinstance(result, dict):
                    ctx.set_details(result)
                return result

        return wrapper

    return decorator
