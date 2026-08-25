"""Tests for the canonical Data health projection."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from finance_sync.schemas.control_plane import (
    ControlPlaneAction,
    ControlPlaneConnection,
    ControlPlaneFreshness,
    ControlPlaneIssue,
    ControlPlaneOverview,
    ControlPlaneSummary,
    InstallationStatus,
)
from finance_sync.schemas.data_health import (
    DataHealthOverview,
    DataHealthSource,
)
from finance_sync.schemas.data_quality import (
    DataQualityCoverage,
    DataQualityOverview,
)
from finance_sync.services.data_health import DataHealthService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


class _Result:
    def __init__(
        self,
        *,
        rows: list[Any] | None = None,
        scalars: list[Any] | None = None,
        scalar: Any = None,
    ) -> None:
        self.rows = rows or []
        self.scalar_rows = scalars or []
        self.scalar_value = scalar

    def all(self) -> list[Any]:
        return self.rows

    def scalars(self) -> _Result:
        return self

    def scalar_one(self) -> Any:
        return self.scalar_value

    def __iter__(self) -> Iterator[Any]:
        return iter(self.scalar_rows)


class _Session:
    def __init__(self, *responses: _Result) -> None:
        self.responses = list(responses)

    async def execute(self, _statement: Any) -> _Result:
        return self.responses.pop(0)


def _action() -> ControlPlaneAction:
    return ControlPlaneAction(
        key="view_data_source",
        label="Bekijken",
        method="GET",
        path="/api/v1/data-source",
    )


def _control(now: datetime) -> ControlPlaneOverview:
    connection = ControlPlaneConnection(
        id="connection-1",
        provider="bunq",
        name="Main",
        status="healthy",
        last_success_at=now,
        last_attempt_at=now,
    )
    return ControlPlaneOverview(
        status="attention_required",
        installation=InstallationStatus(redis="not_configured"),
        summary=ControlPlaneSummary(connections_total=1),
        connections=[connection],
        syncs=[],
        issues=[
            ControlPlaneIssue(
                id="security-unresolved:1",
                severity="warning",
                category="security_mapping",
                title="Security niet herkend",
                description="Een positie kan niet worden gekoppeld.",
                impact_count=2,
                provider="bunq",
                action=_action(),
            ),
            ControlPlaneIssue(
                id="export-failed:1",
                severity="error",
                category="export",
                title="Export mislukt",
                description="De bestemming kon niet worden bijgewerkt.",
                action=_action(),
            ),
        ],
        freshness=ControlPlaneFreshness(
            status="stale",
            securities_stale=3,
            securities_without_quote=1,
        ),
        coverage={"connections_with_data": 1, "connections_total": 1},
        destinations=[],
        as_of=now,
        generated_at=now,
    )


def _quality(now: datetime) -> DataQualityOverview:
    return DataQualityOverview(
        status="attention_required",
        latest_run_id="run-1",
        latest_run_status="completed",
        latest_run_at=now,
        findings_total=1,
        findings_by_kind={"amount_mismatch": 1},
        coverage=[
            DataQualityCoverage(provider="bunq", accounts=2, transactions=12)
        ],
        generated_at=now,
    )


def test_missing_healthy_source_gets_sync_action() -> None:
    connection = ControlPlaneConnection(
        id="connection-empty",
        provider="csv_import",
        name="Empty CSV",
        status="healthy",
        actions=[
            ControlPlaneAction(
                key="sync_connection",
                label="Nu synchroniseren",
                method="POST",
                path="/api/v1/sync/connections/connection-empty",
            )
        ],
    )
    issues = DataHealthService(None, "tenant-a")._missing_source_issues(
        [
            DataHealthSource(
                id="connection-empty", provider="csv_import", status="healthy"
            )
        ],
        SimpleNamespace(connections=[connection]),
    )

    assert len(issues) == 1
    assert issues[0].category == "missing_transactions"
    assert issues[0].action.key == "sync_connection"


def test_empty_installation_gets_missing_source_action() -> None:
    issues = DataHealthService(None, "tenant-a")._missing_source_issues(
        [], SimpleNamespace(connections=[])
    )

    assert len(issues) == 1
    assert issues[0].category == "missing_transactions"
    assert issues[0].action.key == "view_connection"
    assert issues[0].action.path == "/api/v1/connectors/configs"


@pytest.mark.asyncio
async def test_data_health_composes_existing_projections(monkeypatch) -> None:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)

    async def control(self):
        return _control(now)

    async def quality(self):
        return _quality(now)

    monkeypatch.setattr(
        "finance_sync.services.data_health.ControlPlaneService.get_overview",
        control,
    )
    monkeypatch.setattr(
        "finance_sync.services.data_health.DataQualityService.get_overview",
        quality,
    )

    async def no_additional_issues(self, *args):
        return []

    monkeypatch.setattr(
        DataHealthService, "_additional_issues", no_additional_issues
    )
    monkeypatch.setattr(
        DataHealthService,
        "_changed_provider_issues",
        no_additional_issues,
    )

    overview = await DataHealthService(None, "tenant-1", now=now).get_overview()

    assert isinstance(overview, DataHealthOverview)
    assert overview.status == "attention_required"
    assert overview.last_successful_sync == now
    assert overview.sources[0].transactions == 12
    assert overview.stale_data["securities_stale"] == 3
    assert overview.unresolved_securities == 1
    assert overview.failed_exports == 1
    assert overview.reconciliation.findings_by_kind == {"amount_mismatch": 1}
    assert {issue.category for issue in overview.issues} == {
        "unresolved_security",
        "failed_export",
    }
    assert all(
        issue.action.path.startswith("/api/v1/") for issue in overview.issues
    )


@pytest.mark.asyncio
async def test_additional_health_issues_cover_duplicate_balances_and_imports() -> (
    None
):
    run = SimpleNamespace(
        id="import-1",
        status="quarantined",
        rejected_count=2,
        skipped_count=1,
    )
    session = _Session(
        _Result(rows=[("bunq", "account-1", 2, 10, 20)]),
        _Result(scalars=[run]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session),
        "tenant-a",
        permissions={"accounts:read", "connectors:read"},
    )._additional_issues()

    assert [issue.category for issue in issues] == [
        "duplicate_accounts",
        "balance_conflict",
        "incomplete_import",
    ]
    assert issues[0].action.key == "view_accounts"
    assert issues[2].action.path == "/api/v1/connectors/file-uploads/runs"
    assert issues[2].impact_count == 3


@pytest.mark.asyncio
async def test_changed_provider_data_gets_provider_sync_action() -> None:
    session = _Session(_Result(rows=[("bunq", 3)]))
    source = DataHealthSource(
        id="connection-bunq", provider="bunq", status="healthy"
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._changed_provider_issues([source])

    assert len(issues) == 1
    assert issues[0].category == "provider_data_changed"
    assert issues[0].impact_count == 3
    assert issues[0].action.key == "sync_connection"
    assert issues[0].action.path.endswith("connection-bunq")


@pytest.mark.parametrize(
    ("control", "quality", "expected"),
    [
        ("sync_failed", "healthy", "error"),
        ("healthy", "attention_required", "attention_required"),
        ("healthy", "unavailable", "unavailable"),
        ("healthy", "healthy", "healthy"),
    ],
)
def test_data_health_status_precedence(control, quality, expected) -> None:
    assert DataHealthService._status(control, quality) == expected
