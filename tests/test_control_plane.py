"""Contract and deterministic projection tests for phase 1."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from finance_sync.schemas.control_plane import (
    ControlPlaneAction,
    ControlPlaneDestination,
    ControlPlaneFreshness,
    ControlPlaneIssue,
    ControlPlaneOverview,
)
from finance_sync.services.control_plane import ControlPlaneService
from finance_sync.services.control_plane_actions import ACTION_CATALOG, action


class _Result:
    def __init__(
        self,
        *,
        scalar: Any = None,
        scalars: list[Any] | tuple[Any, ...] = (),
        rows: list[Any] | tuple[Any, ...] = (),
    ) -> None:
        self._scalar = scalar
        self._scalars = list(scalars)
        self._rows = list(rows)

    def scalars(self) -> _Result:
        return self

    def __iter__(self):
        return iter(self._scalars)

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(
        self, *responses: _Result, scalar_values: list[Any] = ()
    ) -> None:
        self._responses = list(responses)
        self._scalar_values = list(scalar_values)

    async def execute(self, _statement: Any) -> _Result:
        return self._responses.pop(0)

    async def scalar(self, _statement: Any) -> Any:
        return self._scalar_values.pop(0)


def test_control_plane_issue_always_contains_one_concrete_action() -> None:
    issue = ControlPlaneIssue(
        id="security-unresolved:123",
        severity="warning",
        category="security_mapping",
        title="Security niet herkend",
        description="Een positie moet worden gekoppeld.",
        action=ControlPlaneAction(
            label="Security mappen",
            method="GET",
            path="/api/v1/securities/unresolved",
        ),
    )

    assert issue.action.method == "GET"
    assert issue.action.path.startswith("/api/v1/")


def test_empty_overview_is_a_valid_healthy_contract() -> None:
    generated_at = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    overview = ControlPlaneOverview(
        status="healthy",
        installation={"redis": "not_configured"},
        summary={},
        connections=[],
        syncs=[],
        issues=[],
        freshness={"status": "unavailable"},
        coverage={},
        destinations=[],
        generated_at=generated_at,
    )

    assert overview.summary.connections_total == 0
    assert overview.freshness.status == "unavailable"
    assert overview.generated_at == generated_at


def test_connection_projection_never_decrypts_or_exposes_payload() -> None:
    row = SimpleNamespace(
        id="connection-1",
        provider_key="bunq",
        description='{"_label": "Main bank", "api_key": "secret"}',
        status="active",
        last_error=None,
        last_attempt_at=None,
        last_success_at=None,
    )

    projected = ControlPlaneService._connection(row, None)  # type: ignore[arg-type]

    assert projected.name == "Main bank"
    assert "secret" not in projected.model_dump_json()


def test_empty_file_import_profile_is_pending_not_connection_error() -> None:
    row = SimpleNamespace(
        id="saxo-connection",
        provider_key="saxo_investor",
        description='{"_label": "SaxoInvestor"}',
        encrypted_payload=b"",
        status="active",
        last_error="Sync failed due to an internal error",
        last_error_category="provider_unavailable",
        last_attempt_at=None,
        last_success_at=None,
        last_test_at=None,
        last_test_status=None,
        last_test_error=None,
    )

    projected = ControlPlaneService._connection(row, None)  # type: ignore[arg-type]

    assert projected.status == "pending"
    assert projected.last_error is None
    assert projected.last_error_category is None


def test_action_catalog_has_the_standard_contract() -> None:
    assert {
        "test_connection",
        "sync_connection",
        "view_sync_run",
        "retry_sync",
        "map_security",
        "view_data_source",
        "test_destination",
        "run_export",
        "retry_export",
    } <= ACTION_CATALOG.keys()
    retry = action(
        "retry_sync",
        "/api/v1/sync-runs/run-1/retry",
        permissions={"sync:read"},
    )
    assert retry.enabled is False
    assert retry.permission == "sync:write"
    assert retry.disabled_reason == "Ontbrekende permissie: sync:write"


def test_action_catalog_disables_state_incompatible_actions() -> None:
    sync = action(
        "sync_connection",
        "/api/v1/sync/connections/connection-1",
        permissions={"sync:write"},
        disabled_reason="De verbinding is gepauzeerd.",
    )
    assert sync.enabled is False
    assert sync.destructive is False
    assert sync.disabled_reason == "De verbinding is gepauzeerd."


def test_action_catalog_supports_api_key_wildcards() -> None:
    run = action(
        "run_export",
        "/api/v1/destinations/target-1/run",
        permissions={"destinations:*"},
    )
    assert run.enabled is True
    assert run.destructive is True


def test_phase3_destination_projection_exposes_recovery_actions() -> None:
    assert {
        "preview_destination",
        "configure_destination",
        "pause_destination",
        "retry_export",
    } <= ACTION_CATALOG.keys()
    destination = ControlPlaneDestination(
        id="target-1",
        type="wealthfolio",
        name="Portfolio",
        status="active",
        selected_account_ids=["account-1"],
        last_export_status="failed",
        failed_export_count=2,
        actions=[
            action(
                "retry_export",
                "/api/v1/destinations/destination-1/retry",
            )
        ],
    )
    assert destination.selected_account_ids == ["account-1"]
    assert destination.actions[0].key == "retry_export"


def test_phase3_freshness_carries_valuation_and_source_breakdown() -> None:
    freshness = ControlPlaneFreshness(
        status="partial",
        securities_total=3,
        securities_fresh=2,
        securities_without_quote=1,
        holdings_without_valuation=1,
        by_source={
            "openbb": {"total": 3, "fresh": 2, "stale": 0, "without_quote": 1}
        },
    )
    assert freshness.holdings_without_valuation == 1
    assert freshness.by_source["openbb"]["fresh"] == 2


@pytest.mark.parametrize(
    ("status", "last_error", "last_success_at", "expected"),
    [
        ("paused", None, None, "paused"),
        ("active", "provider down", None, "error"),
        ("active", None, None, "pending"),
        ("active", None, datetime(2026, 8, 24, tzinfo=UTC), "healthy"),
    ],
)
def test_connection_projection_covers_operational_states(
    status: str,
    last_error: str | None,
    last_success_at: datetime | None,
    expected: str,
) -> None:
    row = SimpleNamespace(
        id="connection-1",
        provider_key="bunq",
        description="not-json",
        status=status,
        last_error=last_error,
        last_attempt_at=None,
        last_success_at=last_success_at,
        last_error_category="provider_unavailable",
        last_test_at=None,
        last_test_status=None,
        last_test_error=None,
    )

    projected = ControlPlaneService._connection(
        row, None, {"connectors:write", "sync:write"}
    )

    assert projected.status == expected
    assert projected.name == "bunq"
    assert projected.actions[0].enabled is True
    assert projected.actions[0].permission == "connectors:write"
    if status == "paused":
        assert (
            projected.actions[1].disabled_reason
            == "De verbinding is gepauzeerd."
        )
    else:
        assert projected.actions[1].enabled is True


def test_sync_projection_disables_retry_for_non_failed_runs() -> None:
    row = SimpleNamespace(
        id="run-1",
        connector="bunq",
        connection_id=None,
        status="completed",
        started_at=datetime(2026, 8, 24, tzinfo=UTC),
        completed_at=datetime(2026, 8, 24, 0, 1, tzinfo=UTC),
        items_processed=4,
        error_message=None,
        error_category=None,
        cursor=None,
    )

    projected = ControlPlaneService._sync(row, {"sync:read", "sync:write"})

    assert projected.connection_id is None
    assert projected.actions[0].enabled is True
    assert projected.actions[1].enabled is False
    assert "mis" in (projected.actions[1].disabled_reason or "")


def test_connection_and_sync_issues_include_fallback_descriptions() -> None:
    service = ControlPlaneService(cast("Any", object()), "tenant-a")
    connection = SimpleNamespace(
        id="connection-1", status="error", last_error=None
    )
    sync = SimpleNamespace(id="run-1", status="failed", error_message=None)

    issues = service._connection_issues([connection], [sync])

    assert [issue.id for issue in issues] == [
        "connection-error:connection-1",
        "sync-failed:run-1",
    ]
    assert issues[0].action.key == "edit_connection"
    assert issues[1].action.key == "view_sync_run"


def test_freshness_issues_are_suppressed_only_when_fully_fresh() -> None:
    service = ControlPlaneService(cast("Any", object()), "tenant-a")
    fresh = ControlPlaneFreshness(
        status="fresh", securities_total=2, securities_fresh=2
    )
    incomplete = fresh.model_copy(update={"holdings_without_valuation": 1})

    assert service._freshness_issues(fresh) == []
    assert service._freshness_issues(incomplete)[0].category == "freshness"


def test_destination_issues_cover_health_and_failed_export_paths() -> None:
    service = ControlPlaneService(cast("Any", object()), "tenant-a")
    failed = ControlPlaneDestination(
        id="destination-1",
        type="wealthfolio",
        name="Portfolio",
        status="error",
        health_status="unhealthy",
        last_error=None,
        failed_export_count=2,
        actions=[
            action(
                "retry_export",
                "/api/v1/destinations/destination-1/retry",
            )
        ],
    )

    issues = service._destination_issues([failed])

    assert [issue.category for issue in issues] == ["destination", "export"]
    assert issues[0].action.key == "test_destination"
    assert issues[1].action.key == "retry_export"


@pytest.mark.asyncio
async def test_empty_overview_uses_stable_unavailable_subcontracts() -> None:
    generated = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    session = _Session(
        _Result(scalars=[]),  # credentials
        _Result(scalars=[]),  # freshness rows
        _Result(scalars=[]),  # destinations
        _Result(rows=[]),  # coverage rows
        scalar_values=[None, 0, 0, None],  # reconciliation, freshness x2, as-of
    )

    overview = await ControlPlaneService(
        cast("Any", session),
        "tenant-empty",
        now=generated,
    ).get_overview()

    assert overview.status == "attention_required"
    assert overview.installation.redis == "not_configured"
    assert overview.freshness.status == "unavailable"
    assert overview.coverage.connections_total == 0
    assert overview.summary.issues_open == 1
    assert overview.as_of is None
    assert overview.generated_at == generated


@pytest.mark.asyncio
async def test_freshness_aggregates_sources_stale_quotes_and_missing_valuation() -> (
    None
):
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    freshness_rows = [
        SimpleNamespace(
            last_quote_fetch=now - timedelta(hours=1),
            updated_at=now - timedelta(minutes=5),
            data_source="openbb",
        ),
        SimpleNamespace(
            last_quote_fetch=now - timedelta(days=3),
            updated_at=now - timedelta(days=3),
            data_source="openbb",
        ),
        SimpleNamespace(
            last_quote_fetch=None,
            updated_at=now - timedelta(days=2),
            data_source=None,
        ),
    ]
    session = _Session(_Result(scalars=freshness_rows), scalar_values=[4, 2])

    freshness = await ControlPlaneService(
        cast("Any", session), "tenant-a", now=now
    )._freshness()

    assert freshness.status == "partial"
    assert freshness.securities_total == 4
    assert freshness.securities_fresh == 1
    assert freshness.securities_stale == 1
    assert freshness.securities_without_quote == 2
    assert freshness.holdings_without_valuation == 2
    assert freshness.by_source["openbb"] == {
        "total": 2,
        "fresh": 1,
        "stale": 1,
        "without_quote": 0,
    }
    assert freshness.by_source["unknown"]["without_quote"] == 1
    assert freshness.last_enrichment_at == now - timedelta(minutes=5)


@pytest.mark.asyncio
async def test_coverage_is_tenant_scoped_and_deduplicates_connections() -> None:
    credentials = [
        SimpleNamespace(id="connection-a"),
        SimpleNamespace(id="connection-b"),
    ]
    session = _Session(
        _Result(
            rows=[
                ("connection-a", "bunq"),
                ("connection-a", "bunq"),
                ("connection-b", "csv"),
                (None, "ignored"),
            ]
        )
    )

    coverage = await ControlPlaneService(
        cast("Any", session), "tenant-a"
    )._coverage(credentials)

    assert coverage.connections_with_data == 2
    assert coverage.connections_total == 2
    assert coverage.providers == ["bunq", "csv", "ignored"]


@pytest.mark.asyncio
async def test_destination_projection_includes_schedule_and_retry_action() -> (
    None
):
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    target = SimpleNamespace(
        id="destination-a",
        target_type="wealthfolio",
        display_name="Portfolio",
        status="paused",
        last_health_status="healthy",
        last_checked_at=now,
        last_health_error=None,
        schedule_id="schedule-a",
        selected_account_ids=None,
    )
    schedule = SimpleNamespace(
        id="schedule-a", next_run_at=now + timedelta(hours=1)
    )
    latest = SimpleNamespace(
        id="export-latest",
        status="completed",
        completed_at=now - timedelta(minutes=2),
    )
    failed = SimpleNamespace(id="export-failed")
    session = _Session(
        _Result(scalars=[target]),
        _Result(scalars=[schedule]),
        scalar_values=[latest, failed, 2],
    )

    destinations = await ControlPlaneService(
        cast("Any", session), "tenant-a", permissions={"destinations:read"}
    )._destinations()

    destination = destinations[0]
    assert destination.next_scheduled_at == schedule.next_run_at
    assert destination.selected_account_ids == []
    assert destination.last_export_status == "completed"
    assert destination.failed_export_count == 2
    assert {item.key for item in destination.actions} >= {
        "test_destination",
        "run_export",
        "retry_export",
    }
    assert (
        next(
            item for item in destination.actions if item.key == "run_export"
        ).enabled
        is False
    )
    assert (
        next(
            item
            for item in destination.actions
            if item.key == "pause_destination"
        ).disabled_reason
        == "De bestemming is al gepauzeerd."
    )


@pytest.mark.asyncio
async def test_security_issue_contains_candidates_confidence_and_impact() -> (
    None
):
    unresolved = SimpleNamespace(
        id="unresolved-a",
        provider_key="bunq",
        external_security_id="external-a",
        raw_isin=None,
        raw_figi=None,
        raw_ticker="E2E",
        raw_name="Example Security",
    )
    candidate = SimpleNamespace(
        id="security-a",
        name="Example Security",
        ticker="E2E",
        isin=None,
        figi=None,
    )
    session = _Session(
        _Result(scalars=[unresolved]),
        _Result(scalars=[candidate]),
        scalar_values=[3, 2],
    )

    issues = await ControlPlaneService(
        cast("Any", session),
        "tenant-a",
        permissions={"securities:write"},
    )._security_issues([SimpleNamespace(provider_key="bunq")])

    assert len(issues) == 1
    issue = issues[0]
    assert issue.id == "security-unresolved:unresolved-a"
    assert issue.impact_count == 5
    assert issue.confidence == "high"
    assert issue.candidate_securities[0]["security_id"] == "security-a"
    assert issue.action.key == "map_security"
    assert issue.action.enabled is True


@pytest.mark.asyncio
async def test_reconciliation_issue_feed_is_latest_run_scoped() -> None:
    latest = SimpleNamespace(id="reconciliation-a")
    result = SimpleNamespace(
        id="finding-a",
        severity="error",
        description=None,
        provider_key="bunq",
        transaction_id_a="transaction-a",
        transaction_id_b=None,
    )
    session = _Session(
        _Result(scalars=[result]),
        scalar_values=[latest],
    )

    issues = await ControlPlaneService(
        cast("Any", session), "tenant-a"
    )._reconciliation_issues()

    assert len(issues) == 1
    assert issues[0].id == "reconciliation:finding-a"
    assert issues[0].severity == "error"
    assert issues[0].description == "Controleer de finding."
    assert issues[0].impact_count == 1
    assert issues[0].action.path == "/api/v1/reconciliation/reconciliation-a"


@pytest.mark.asyncio
async def test_schedule_and_sync_loaders_preserve_connection_scope() -> None:
    schedule = SimpleNamespace(target_id="connection-a")
    sync = SimpleNamespace(id="run-a")
    session = _Session(
        _Result(scalars=[schedule]),
        _Result(scalars=[sync]),
    )
    service = ControlPlaneService(cast("Any", session), "tenant-a")

    schedules = await service._load_schedules(["connection-a"])
    syncs = await service._load_syncs(["connection-a"])

    assert schedules == {"connection-a": schedule}
    assert syncs == [sync]
    assert await service._load_schedules([]) == {}
    assert await service._load_syncs([]) == []


def test_connection_label_handles_invalid_and_missing_descriptions() -> None:
    assert (
        ControlPlaneService._label(
            SimpleNamespace(
                provider_key="bunq", description='{"_label": " Main "}'
            )
        )
        == " Main "
    )
    assert (
        ControlPlaneService._label(
            SimpleNamespace(provider_key="csv", description="invalid")
        )
        == "csv"
    )
