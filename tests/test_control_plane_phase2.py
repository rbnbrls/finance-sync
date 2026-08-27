"""Regression tests for phase-2 recovery contracts."""

from datetime import UTC, datetime, timedelta

from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.models.sync_run import SyncRun
from finance_sync.schemas.control_plane import ControlPlaneSync
from finance_sync.sync.errors import categorize_sync_error


def test_sync_error_categories_are_stable() -> None:
    assert categorize_sync_error(RateLimitError()) == "rate_limited"
    assert categorize_sync_error(TransientError("provider down")) == (
        "provider_unavailable"
    )
    assert categorize_sync_error(PermanentError("invalid credentials")) == (
        "authentication"
    )
    assert categorize_sync_error(
        PermanentError("invalid security mapping")
    ) == ("data_mapping")
    # 4xx client errors (e.g. invalid ``since`` → HTTP 400) are now
    # classified as PermanentError and categorised as validation, so
    # operators see the actionable cause instead of "provider_unavailable".
    assert categorize_sync_error(
        PermanentError("Trading212 request failed (HTTP 400)")
    ) == ("validation")


def test_sync_projection_contains_recovery_metadata() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    run = SyncRun(
        connector="bunq",
        connection_id="connection-1",
        status="failed",
        started_at=started,
        completed_at=started + timedelta(seconds=4),
        items_processed=12,
        cursor=started,
        error_category="authentication",
        error_message="credentials rejected",
    )
    projection = ControlPlaneSync(
        id="run-1",
        connector=run.connector,
        connection_id=run.connection_id,
        status=str(run.status),
        started_at=run.started_at,
        completed_at=run.completed_at,
        items_processed=run.items_processed,
        cursor=run.cursor,
        error_category=run.error_category,
        error_message=run.error_message,
    )
    assert projection.error_category == "authentication"
    assert projection.cursor == started
