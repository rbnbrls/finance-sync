"""Phase-0 tests for the control-plane vocabulary and decision rules."""

from datetime import UTC, datetime, timedelta

import pytest

from finance_sync.control_plane_contract import (
    CONTROL_PLANE_ERROR_CATEGORIES,
    latest_timestamp,
    overview_status,
)


@pytest.mark.parametrize(
    (
        "failed_syncs",
        "issues_open",
        "failed_destinations",
        "freshness",
        "expected",
    ),
    [
        (1, 4, 1, "unavailable", "sync_failed"),
        (0, 1, 0, "fresh", "attention_required"),
        (0, 0, 1, "fresh", "attention_required"),
        (0, 0, 0, "partial", "partial"),
        (0, 0, 0, "unavailable", "partial"),
        (0, 0, 0, "fresh", "healthy"),
        (0, 0, 0, "stale", "healthy"),
    ],
)
def test_overview_status_has_deterministic_precedence(
    failed_syncs: int,
    issues_open: int,
    failed_destinations: int,
    freshness: str,
    expected: str,
) -> None:
    assert (
        overview_status(
            failed_syncs=failed_syncs,
            issues_open=issues_open,
            failed_destinations=failed_destinations,
            freshness_status=freshness,  # type: ignore[arg-type]
        )
        == expected
    )


def test_latest_timestamp_ignores_missing_source_data() -> None:
    older = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    newer = older + timedelta(minutes=5)

    assert latest_timestamp([None, older, newer, None]) == newer
    assert latest_timestamp([None, None]) is None


def test_error_category_contract_is_closed() -> None:
    assert {
        "authentication",
        "provider_unavailable",
        "rate_limited",
        "validation",
        "data_mapping",
        "database",
        "unknown",
    } == CONTROL_PLANE_ERROR_CATEGORIES
