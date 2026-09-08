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
from finance_sync.services.wealthfolio_preflight import (
    validate_activity_semantics,
)

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
async def test_account_identity_health_detects_legacy_trading212_duplicate() -> (
    None
):
    connection_id = "20ac3d72-0000-4000-8000-000000000001"
    session = _Session(
        _Result(
            scalars=[
                SimpleNamespace(
                    id="account-legacy",
                    provider_key="trading212",
                    external_account_id="trading212",
                    connection_id=None,
                    currency_code="EUR",
                ),
                SimpleNamespace(
                    id="account-current",
                    provider_key="trading212",
                    external_account_id="12345678",
                    connection_id=connection_id,
                    currency_code="EUR",
                ),
            ]
        )
    )

    issues = await DataHealthService(
        cast("AsyncSession", session),
        "tenant-a",
        permissions={"accounts:read"},
    )._account_identity_issues()

    assert len(issues) == 1
    assert issues[0].category == "account_identity_conflict"
    assert issues[0].severity == "error"
    assert issues[0].blocking is True
    assert issues[0].account_ids == ["account-legacy", "account-current"]
    assert issues[0].evidence["connection_ids"] == [
        connection_id,
        "legacy",
    ]


@pytest.mark.asyncio
async def test_account_metadata_identity_health_redacts_provider_value() -> (
    None
):
    session = _Session(
        _Result(
            scalars=[
                SimpleNamespace(
                    id="account-1",
                    provider_key="bunq",
                    external_account_id="provider-a",
                    provider_metadata={"iban": "NL00BANK0123456789"},
                ),
                SimpleNamespace(
                    id="account-2",
                    provider_key="bunq",
                    external_account_id="provider-b",
                    provider_metadata={"iban": "NL00 BANK 0123456789"},
                ),
            ]
        )
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a", permissions={"accounts:read"}
    )._account_metadata_identity_issues()

    assert len(issues) == 1
    assert issues[0].category == "account_identity_conflict"
    assert issues[0].severity == "warning"
    assert issues[0].account_ids == ["account-1", "account-2"]
    assert "NL00BANK0123456789" not in str(issues[0].model_dump())
    assert issues[0].evidence["metadata_key"] == "iban"
    assert issues[0].evidence["identity_hash"]


@pytest.mark.asyncio
async def test_selected_account_health_detects_missing_local_account() -> None:
    connection_id = "connection-1"
    session = _Session(
        _Result(
            scalars=[
                SimpleNamespace(
                    id=connection_id,
                    provider_key="trading212",
                    status="active",
                    selected_accounts=["present", "missing"],
                )
            ]
        ),
        _Result(
            scalars=[
                SimpleNamespace(
                    id="account-1",
                    connection_id=connection_id,
                    provider_key="trading212",
                    external_account_id="present",
                )
            ]
        ),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a", permissions={"connectors:read"}
    )._selected_account_issues()

    assert len(issues) == 1
    assert issues[0].category == "account_identity_conflict"
    assert issues[0].connection_id == connection_id
    assert issues[0].impact_count == 1
    assert issues[0].evidence["missing_external_account_ids"] == ["missing"]
    assert issues[0].action.key == "view_connection"
    assert issues[0].action.path == f"/api/v1/connectors/configs/{connection_id}"


@pytest.mark.asyncio
async def test_orphaned_account_health_detects_missing_connection() -> None:
    connection_id = "deleted-connection"
    session = _Session(
        _Result(
            scalars=[
                SimpleNamespace(
                    id="account-orphan",
                    connection_id=connection_id,
                    provider_key="bunq",
                )
            ]
        ),
        _Result(scalars=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a", permissions={"accounts:read"}
    )._orphaned_account_issues()

    assert len(issues) == 1
    assert issues[0].category == "account_identity_conflict"
    assert issues[0].account_ids == ["account-orphan"]
    assert issues[0].connection_id == connection_id
    assert issues[0].evidence["reason"] == "missing_connection"


@pytest.mark.asyncio
async def test_account_metadata_identity_health_accepts_provider_aliases() -> (
    None
):
    session = _Session(
        _Result(
            scalars=[
                SimpleNamespace(
                    id="account-1",
                    provider_key="broker",
                    external_account_id="legacy-a",
                    provider_metadata={
                        "accountId": "BROKER-123456",
                        "account_id": "broker 123456",
                    },
                ),
                SimpleNamespace(
                    id="account-2",
                    provider_key="broker",
                    external_account_id="current-b",
                    provider_metadata={"account_id": "broker 123456"},
                ),
            ]
        )
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a", permissions={"accounts:read"}
    )._account_metadata_identity_issues()

    assert len(issues) == 1
    assert issues[0].evidence["metadata_key"] == "account_id"
    assert issues[0].account_ids == ["account-1", "account-2"]


@pytest.mark.asyncio
async def test_account_metadata_identity_health_accepts_account_provider_aliases() -> (
    None
):
    session = _Session(
        _Result(
            scalars=[
                SimpleNamespace(
                    id="account-1",
                    provider_key="bunq",
                    external_account_id="legacy-a",
                    provider_metadata={"monetaryAccountId": "1000001"},
                ),
                SimpleNamespace(
                    id="account-2",
                    provider_key="bunq",
                    external_account_id="current-b",
                    provider_metadata={"monetary_account_id": "1000001"},
                ),
            ]
        )
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._account_metadata_identity_issues()

    assert len(issues) == 1
    assert issues[0].evidence["metadata_key"] == "monetary_account_id"
    assert issues[0].account_ids == ["account-1", "account-2"]


@pytest.mark.asyncio
async def test_transaction_identity_health_detects_cross_connection_duplicate() -> (
    None
):
    session = _Session(_Result(rows=[("trading212", "provider-tx-1", 2)]))

    issues = await DataHealthService(
        cast("AsyncSession", session),
        "tenant-a",
        permissions={"transactions:read"},
    )._transaction_identity_issues()

    assert len(issues) == 1
    assert issues[0].category == "duplicate_transaction_identity"
    assert issues[0].impact_count == 2
    assert issues[0].affected_record_count == 2
    assert issues[0].evidence == {
        "external_transaction_id": "provider-tx-1",
        "total_count": 2,
        "detail_limit": 0,
    }
    assert issues[0].action.key == "view_transactions"


@pytest.mark.asyncio
async def test_transaction_fingerprint_health_redacts_fingerprint() -> None:
    session = _Session(
        _Result(rows=[("trading212", "secret-provider-fingerprint", 2)])
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._transaction_fingerprint_issues()

    assert len(issues) == 1
    assert issues[0].category == "duplicate_transaction_identity"
    assert "secret-provider-fingerprint" not in str(issues[0].model_dump())
    assert issues[0].evidence["fingerprint_hash"]


@pytest.mark.asyncio
async def test_transaction_semantic_duplicate_health_hashes_identity() -> None:
    session = _Session(
        _Result(
            rows=[
                (
                    "trading212",
                    "account-1",
                    "2026-08-25",
                    "purchase",
                    100,
                    "EUR",
                    2,
                    "security-1",
                    2,
                )
            ]
        )
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._transaction_semantic_duplicate_issues()

    assert len(issues) == 1
    assert issues[0].category == "duplicate_transaction_identity"
    assert issues[0].account_ids == ["account-1"]
    assert issues[0].impact_count == 2
    assert issues[0].evidence["semantic_key_hash"]
    assert "100" not in str(issues[0].model_dump())


@pytest.mark.asyncio
async def test_transaction_relationship_health_detects_connection_mismatch() -> (
    None
):
    session = _Session(
        _Result(
            rows=[
                (
                    "tx-1",
                    "trading212",
                    "connection-a",
                    "account-1",
                    "trading212",
                    "connection-b",
                )
            ]
        ),
        _Result(rows=[]),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._transaction_relationship_issues()

    assert len(issues) == 1
    assert issues[0].category == "account_identity_conflict"
    assert issues[0].blocking is True
    assert issues[0].affected_transaction_ids == ["tx-1"]
    assert issues[0].evidence == {"total_count": 1, "detail_limit": 100}


@pytest.mark.asyncio
async def test_sync_integrity_health_detects_orphan_cursor_and_partial_run() -> (
    None
):
    orphan_cursor = SimpleNamespace(
        id="cursor-1",
        connection_id="deleted-connection",
        connector="trading212",
        resource="12345678",
    )
    partial_run = SimpleNamespace(
        id="run-1",
        report={"failed": 2, "skipped": 1},
        warnings=["one account skipped"],
    )
    session = _Session(
        _Result(rows=[(orphan_cursor, None)]),
        _Result(
            rows=[(partial_run, SimpleNamespace(provider_key="trading212"))]
        ),
        _Result(scalar=1),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._sync_integrity_issues()

    assert [issue.category for issue in issues] == [
        "partial_sync",
        "partial_sync",
    ]
    assert issues[0].blocking is True
    assert "cursor-1" in issues[0].details[0]
    assert issues[0].evidence == {"total_count": 1, "detail_limit": 100}
    assert issues[1].severity == "warning"
    assert "failed=2" in issues[1].details[0]
    assert issues[1].impact_count == 1
    assert issues[1].evidence == {"total_count": 1, "detail_limit": 100}


@pytest.mark.asyncio
async def test_sync_integrity_health_detects_stale_cursor_against_latest_run() -> None:
    old_cursor = datetime(2026, 8, 20, tzinfo=UTC)
    latest_run_cursor = datetime(2026, 8, 25, tzinfo=UTC)
    cursor = SimpleNamespace(
        id="cursor-stale",
        connection_id="connection-1",
        connector="trading212",
        resource="12345678",
        cursor=old_cursor,
    )
    credential = SimpleNamespace(id="connection-1", provider_key="trading212")
    run = SimpleNamespace(
        id="run-latest",
        cursor=latest_run_cursor,
        report={},
        warnings=[],
    )
    session = _Session(
        _Result(rows=[(cursor, credential)]),
        _Result(rows=[(run, credential)]),
        _Result(scalar=0),
        _Result(rows=[("connection-1", latest_run_cursor)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._sync_integrity_issues()

    assert len(issues) == 1
    assert issues[0].id.startswith("sync-stale-cursor:")
    assert issues[0].severity == "warning"
    assert issues[0].blocking is False
    assert "cursor-stale" in issues[0].details[0]


@pytest.mark.asyncio
async def test_sync_integrity_health_detects_selected_account_missing_from_run() -> None:
    credential = SimpleNamespace(
        id="connection-1",
        provider_key="trading212",
        selected_accounts=["present", "missing"],
    )
    run = SimpleNamespace(
        id="run-selection-gap",
        cursor=datetime(2026, 8, 25, tzinfo=UTC),
        report={"account_external_ids": ["present"]},
        warnings=[],
    )
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[(run, credential)]),
        _Result(scalar=0),
        _Result(rows=[("connection-1", datetime(2026, 8, 25, tzinfo=UTC))]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._sync_integrity_issues()

    assert len(issues) == 1
    assert issues[0].id.startswith("sync-selected-account-gap:")
    assert issues[0].evidence["missing_external_account_ids"] == ["missing"]
    assert issues[0].connection_id == "connection-1"


@pytest.mark.asyncio
async def test_portfolio_quantity_health_detects_trade_holding_mismatch() -> (
    None
):
    session = _Session(
        _Result(
            rows=[
                (
                    "transaction-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                    100,
                    "2026-08-01",
                    None,
                )
            ]
        ),
        _Result(rows=[("account-1", "security-1", 8, "2026-09-01")]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._portfolio_quantity_issues()

    assert len(issues) == 1
    assert issues[0].category == "portfolio_quantity_mismatch"
    assert issues[0].blocking is True
    assert issues[0].evidence["expected_quantity"] == "10"
    assert issues[0].evidence["actual_quantity"] == "8"
    assert issues[0].evidence["percentage_difference"] == "20"
    assert issues[0].evidence["last_activity_at"] == "2026-08-01"
    assert issues[0].evidence["last_holding_at"] == "2026-09-01"


@pytest.mark.asyncio
async def test_portfolio_quantity_health_skips_invalid_numeric_activity() -> None:
    session = _Session(
        _Result(
            rows=[
                (
                    "transaction-invalid-quantity",
                    "account-1",
                    "security-1",
                    "purchase",
                    "NaN",
                    -100,
                    "2026-08-01",
                    None,
                )
            ]
        ),
        _Result(rows=[("account-1", "security-1", 10, "2026-09-01")]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._portfolio_quantity_issues()

    assert issues == []


@pytest.mark.asyncio
async def test_portfolio_quantity_health_includes_security_transfers() -> None:
    session = _Session(
        _Result(
            rows=[
                (
                    "tx-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                    1000,
                    "2026-08-01",
                    None,
                ),
                (
                    "tx-2",
                    "account-1",
                    "security-1",
                    "transfer",
                    3,
                    300,
                    "2026-08-02",
                    None,
                ),
                (
                    "tx-3",
                    "account-1",
                    "security-1",
                    "transfer",
                    2,
                    -200,
                    "2026-08-03",
                    None,
                ),
            ]
        ),
        _Result(rows=[("account-1", "security-1", 11, "2026-09-01")]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._portfolio_quantity_issues()

    assert issues == []


@pytest.mark.asyncio
async def test_portfolio_quantity_health_applies_explicit_split_ratio() -> None:
    session = _Session(
        _Result(
            rows=[
                (
                    "tx-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                    1000,
                    "2026-08-01",
                    None,
                ),
                (
                    "tx-2",
                    "account-1",
                    "security-1",
                    "split",
                    None,
                    0,
                    "2026-08-02",
                    {"split_ratio": "2"},
                ),
            ]
        ),
        _Result(rows=[("account-1", "security-1", 20, "2026-09-01")]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._portfolio_quantity_issues()

    assert issues == []


@pytest.mark.asyncio
async def test_portfolio_quantity_health_reports_unmodeled_corporate_action() -> (
    None
):
    session = _Session(
        _Result(
            rows=[
                (
                    "tx-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                    1000,
                    "2026-08-01",
                    None,
                ),
                (
                    "tx-2",
                    "account-1",
                    "security-1",
                    "corporate_action",
                    None,
                    0,
                    "2026-08-02",
                    {"event": "spin-off"},
                ),
            ]
        ),
        _Result(rows=[("account-1", "security-1", 10, "2026-09-01")]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._portfolio_quantity_issues()

    assert len(issues) == 1
    assert issues[0].category == "invalid_activity_semantics"
    assert issues[0].evidence["insufficient_evidence"] is True
    assert issues[0].evidence["total_count"] == 1
    assert issues[0].evidence["detail_limit"] == 100


@pytest.mark.asyncio
async def test_portfolio_quantity_health_does_not_infer_split_from_zero_basis() -> (
    None
):
    """A split without an opening quantity is evidence-gap, not mismatch."""
    session = _Session(
        _Result(
            rows=[
                (
                    "tx-3",
                    "account-1",
                    "security-1",
                    "corporate_action",
                    None,
                    0,
                    "2026-08-02",
                    {"split_ratio": "2"},
                )
            ]
        ),
        _Result(rows=[("account-1", "security-1", 20, "2026-09-01")]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._portfolio_quantity_issues()

    assert len(issues) == 1
    assert issues[0].category == "invalid_activity_semantics"
    assert issues[0].blocking is False
    assert issues[0].evidence["insufficient_evidence"] is True


@pytest.mark.asyncio
async def test_security_identity_health_detects_ticker_identity_conflict() -> (
    None
):
    session = _Session(
        _Result(
            rows=[
                ("security-1", "US0378331005", "ACME", "stock", "EUR"),
                ("security-2", "US5949181045", "ACME", "stock", "EUR"),
            ]
        ),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._security_identity_issues()

    assert len(issues) == 1
    assert issues[0].category == "security_identity_conflict"
    assert issues[0].severity == "warning"
    assert set(issues[0].security_ids) == {"security-1", "security-2"}


@pytest.mark.asyncio
async def test_security_identity_health_warns_on_cross_variant_ticker() -> None:
    session = _Session(
        _Result(
            rows=[
                ("security-1", "US0378331005", "ACME", "stock", "EUR"),
                ("security-2", "US5949181045", "ACME", "etf", "USD"),
            ]
        ),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._security_identity_issues()

    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].evidence == {
        "ticker": "ACME",
        "variant_count": 2,
        "currencies": ["EUR", "USD"],
        "security_types": ["etf", "stock"],
    }


@pytest.mark.asyncio
async def test_security_identity_health_includes_listing_venue_evidence() -> None:
    session = _Session(
        _Result(
            rows=[
                ("security-1", "US0378331005", "ACME", "stock", "EUR"),
                ("security-2", "US5949181045", "ACME", "etf", "USD"),
            ]
        ),
        _Result(
            rows=[
                ("security-1", "XAMS", "Euronext Amsterdam", "ACME", "EUR"),
                ("security-2", "XNYS", "NYSE", "ACME", "USD"),
            ]
        ),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._security_identity_issues()

    assert len(issues) == 1
    assert issues[0].evidence["listing_venues"] == ["XAMS", "XNYS"]
    assert issues[0].evidence["listing_count"] == 2


@pytest.mark.asyncio
async def test_security_identity_health_blocks_malformed_isin() -> None:
    session = _Session(
        _Result(rows=[("security-1", "not-an-isin", "ACME", "stock", "EUR")]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._security_identity_issues()

    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].blocking is True


@pytest.mark.asyncio
async def test_security_identity_health_blocks_non_iso_currency_code() -> None:
    session = _Session(
        _Result(rows=[("security-1", "US0378331005", "ACME", "stock", "12$")]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._security_identity_issues()

    assert len(issues) == 1
    assert issues[0].evidence == {"identifier_type": "currency_code"}
    assert issues[0].severity == "error"
    assert issues[0].blocking is True


@pytest.mark.asyncio
async def test_destination_parity_reports_partial_wealthfolio_delivery() -> (
    None
):
    run = SimpleNamespace(
        id="export-1",
        status="completed",
        transactions_attempted=10,
        transactions_exported=8,
        transactions_failed=2,
        preflight_manifest={
            "status": "ready",
            "post_export": {"status": "degraded"},
        },
    )
    session = _Session(
        _Result(scalars=[]), _Result(scalars=[]), _Result(scalars=[run])
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._destination_parity_issues()

    assert len(issues) == 1
    assert issues[0].category == "destination_drift"
    assert issues[0].blocking is True
    assert issues[0].evidence["transactions_exported"] == 8


@pytest.mark.asyncio
async def test_tombstoned_export_health_flags_delivered_transaction() -> None:
    session = _Session(
        _Result(
            rows=[
                (
                    "transaction-1",
                    "account-1",
                    datetime(2026, 8, 19, tzinfo=UTC),
                    datetime(2026, 8, 22, tzinfo=UTC),
                    "target-1",
                    datetime(2026, 8, 21, tzinfo=UTC),
                )
            ]
        )
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a", permissions={"destinations:write"}
    )._tombstoned_export_issues()

    assert len(issues) == 1
    assert issues[0].category == "destination_drift"
    assert issues[0].severity == "warning"
    assert issues[0].affected_transaction_ids == ["transaction-1"]
    assert issues[0].evidence["remote_verification_required"] is True
    assert issues[0].action.path == "/api/v1/destinations/target-1/test"


@pytest.mark.asyncio
async def test_destination_parity_reports_incomplete_account_mapping() -> None:
    mapping = SimpleNamespace(
        account_id="account-1",
        provider_account_id="provider-account-1",
        wf_account_id=None,
    )
    session = _Session(
        _Result(scalars=[]), _Result(scalars=[mapping]), _Result(scalars=[])
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._destination_parity_issues()

    assert len(issues) == 1
    assert issues[0].category == "destination_drift"
    assert issues[0].blocking is False
    assert issues[0].account_ids == ["account-1"]


@pytest.mark.asyncio
async def test_destination_mapping_conflicts_are_scoped_to_target() -> None:
    mappings = [
        SimpleNamespace(
            target_id="target-a",
            account_id="account-a",
            provider_account_id="provider-account",
            wf_account_id="remote-a",
        ),
        SimpleNamespace(
            target_id="target-b",
            account_id="account-b",
            provider_account_id="provider-account",
            wf_account_id="remote-b",
        ),
    ]
    session = _Session(
        _Result(scalars=[]), _Result(scalars=mappings), _Result(scalars=[])
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._destination_parity_issues()

    assert issues == []


@pytest.mark.asyncio
async def test_destination_health_status_is_projected_as_actionable_issue() -> (
    None
):
    target = SimpleNamespace(
        id="target-1",
        last_health_status="unauthorized",
        last_health_error="authentication failed",
        last_parity_summary={
            "status": "unauthorized",
            "counts": {
                "remote_accounts": 2,
                "unmapped_remote_accounts": 1,
                "stale_remote_activities": 3,
                "secret": "must-not-pass",
            },
        },
    )
    session = _Session(
        _Result(scalars=[target]), _Result(scalars=[]), _Result(scalars=[])
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._destination_parity_issues()

    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].blocking is True
    assert issues[0].action.key == "test_destination"
    assert issues[0].evidence["parity_counts"] == {
        "remote_accounts": 2,
        "unmapped_remote_accounts": 1,
        "stale_remote_activities": 3,
    }
    assert "secret" not in cast(
        "dict[str, Any]", issues[0].evidence["parity_counts"]
    )


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_closed_positive_lot() -> None:
    session = _Session(
        _Result(
            scalars=[
                SimpleNamespace(
                    id="lot-1",
                    account_id="account-1",
                    security_id="security-1",
                    quantity=10,
                    remaining_quantity=2,
                    closed_at="2026-09-01",
                )
            ]
        ),
        _Result(rows=[]),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].category == "tax_lot_integrity"
    assert issues[0].impact_count == 1
    assert "lot-1" in issues[0].details[0]
    assert issues[0].evidence == {
        "total_count": 1,
        "detail_limit": 100,
        "broken_lot_count": 1,
        "oversold_group_count": 0,
        "missing_basis_count": 0,
    }


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_invalid_cost_basis() -> None:
    lot = SimpleNamespace(
        id="lot-cost-basis",
        account_id="account-1",
        security_id="security-1",
        quantity=10,
        remaining_quantity=10,
        closed_at=None,
        purchase_transaction_id="purchase-1",
        sale_transaction_id=None,
        cost_basis_total=-1,
        cost_basis_per_unit=-0.1,
        currency_code="EUR",
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(
            rows=[
                (
                    "purchase-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].blocking is True
    assert issues[0].evidence["cost_basis_error_count"] == 1
    assert "cost_basis_error=negative_cost_basis_total" in issues[0].details[-1]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_non_finite_quantity() -> None:
    lot = SimpleNamespace(
        id="lot-invalid-quantity",
        account_id="account-1",
        security_id="security-1",
        quantity="NaN",
        remaining_quantity=1,
        closed_at=None,
        purchase_transaction_id="purchase-1",
        sale_transaction_id=None,
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(
            rows=[
                (
                    "purchase-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    1,
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].blocking is True
    assert issues[0].evidence["quantity_error_count"] == 1
    assert "quantity_error=invalid_quantity" in issues[0].details[-1]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_invalid_sale_quantity() -> None:
    session = _Session(
        _Result(scalars=[]),
        _Result(
            rows=[
                (
                    "sale-invalid-quantity",
                    "account-1",
                    "security-1",
                    "sale",
                    "NaN",
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].blocking is True
    assert issues[0].evidence["invalid_sale_quantity_count"] == 1
    assert "sale-invalid-quantity" in issues[0].details[0]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_requires_purchase_link_type() -> None:
    lot = SimpleNamespace(
        id="lot-wrong-link-type",
        account_id="account-1",
        security_id="security-1",
        quantity=10,
        remaining_quantity=10,
        closed_at=None,
        purchase_transaction_id="sale-1",
        sale_transaction_id=None,
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(
            rows=[
                (
                    "sale-1",
                    "account-1",
                    "security-1",
                    "sale",
                    1,
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].blocking is True
    assert "lot-wrong-link-type" in issues[0].details[0]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_open_zero_remaining_lot() -> None:
    lot = SimpleNamespace(
        id="lot-open-zero",
        account_id="account-1",
        security_id="security-1",
        quantity=10,
        remaining_quantity=0,
        closed_at=None,
        purchase_transaction_id="purchase-1",
        sale_transaction_id=None,
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(
            rows=[
                (
                    "purchase-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].blocking is True
    assert "lot-open-zero" in issues[0].details[0]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_zero_quantity_lot() -> None:
    lot = SimpleNamespace(
        id="lot-zero-quantity",
        account_id="account-1",
        security_id="security-1",
        quantity=0,
        remaining_quantity=0,
        closed_at=None,
        purchase_transaction_id=None,
        sale_transaction_id=None,
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(rows=[]),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].blocking is True
    assert issues[0].evidence["quantity_error_count"] == 1
    assert "quantity_error=non_positive_quantity" in issues[0].details[-1]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_cost_basis_unit_mismatch() -> None:
    lot = SimpleNamespace(
        id="lot-cost-basis-mismatch",
        account_id="account-1",
        security_id="security-1",
        quantity=10,
        remaining_quantity=10,
        closed_at=None,
        purchase_transaction_id="purchase-1",
        sale_transaction_id=None,
        cost_basis_total=100,
        cost_basis_per_unit=9,
        currency_code="EUR",
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(
            rows=[
                (
                    "purchase-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].evidence["cost_basis_error_count"] == 1
    assert "cost_basis_error=cost_basis_unit_mismatch" in issues[0].details[-1]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_detects_sale_above_lot_capacity() -> None:
    lot = SimpleNamespace(
        id="lot-1",
        account_id="account-1",
        security_id="security-1",
        quantity=10,
        remaining_quantity=0,
        closed_at="2026-09-01",
        purchase_transaction_id="purchase-1",
        sale_transaction_id=None,
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(
            rows=[
                (
                    "purchase-1",
                    "account-1",
                    "security-1",
                    "purchase",
                    10,
                ),
                (
                    "sale-1",
                    "account-1",
                    "security-1",
                    "sale",
                    12,
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].category == "tax_lot_integrity"
    assert issues[0].impact_count == 1
    assert "sales=12" in issues[0].details[-1]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_warns_on_missing_purchase_basis() -> None:
    lot = SimpleNamespace(
        id="lot-opening",
        account_id="account-1",
        security_id="security-1",
        quantity=10,
        remaining_quantity=10,
        closed_at=None,
        purchase_transaction_id=None,
        sale_transaction_id=None,
    )
    session = _Session(
        _Result(scalars=[lot]),
        _Result(rows=[]),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].blocking is False
    assert "purchase_transaction=missing" in issues[0].details[0]


@pytest.mark.asyncio
async def test_tax_lot_integrity_health_warns_on_sale_without_lot_basis() -> None:
    session = _Session(
        _Result(scalars=[]),
        _Result(
            rows=[
                (
                    "sale-opening",
                    "account-1",
                    "security-1",
                    "sale",
                    3,
                )
            ]
        ),
        _Result(rows=[("account-1",)]),
        _Result(rows=[("security-1",)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].blocking is False
    assert issues[0].impact_count == 1
    assert issues[0].evidence["insufficient_evidence"] is True
    assert "lot_capacity=missing" in issues[0].details[0]


@pytest.mark.asyncio
async def test_cash_reconciliation_health_detects_balance_snapshot_mismatch() -> (
    None
):
    account = SimpleNamespace(
        id="account-1",
        current_balance=100,
        currency_code="EUR",
    )
    balance = SimpleNamespace(
        id="balance-1",
        account_id="account-1",
        balance_kind="current",
        amount=125,
        currency_code="EUR",
        observed_at="2026-09-01",
    )
    session = _Session(
        _Result(scalars=[account]),
        _Result(scalars=[balance]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._cash_reconciliation_issues()

    assert len(issues) == 1
    assert issues[0].category == "cash_reconciliation_mismatch"
    assert issues[0].blocking is True
    assert issues[0].evidence["difference"] == "25"


@pytest.mark.asyncio
async def test_cash_reconciliation_health_surfaces_missing_snapshot_as_evidence_gap():
    account = SimpleNamespace(
        id="account-1",
        current_balance=100,
        currency_code="EUR",
    )
    session = _Session(
        _Result(scalars=[account]),
        _Result(scalars=[]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._cash_reconciliation_issues()

    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].blocking is False
    assert issues[0].evidence == {
        "insufficient_evidence": True,
        "reason": "missing_balance_snapshot",
        "currency": "EUR",
        "account_balance": "100",
        "total_count": 1,
        "detail_limit": 0,
    }


@pytest.mark.asyncio
async def test_cash_reconciliation_health_reconciles_transaction_flow() -> None:
    account = SimpleNamespace(
        id="account-1",
        current_balance=140,
        currency_code="EUR",
    )
    opening = SimpleNamespace(
        id="balance-opening",
        account_id="account-1",
        balance_kind="current",
        amount=100,
        currency_code="EUR",
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    closing = SimpleNamespace(
        id="balance-closing",
        account_id="account-1",
        balance_kind="current",
        amount=140,
        currency_code="EUR",
        observed_at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    session = _Session(
        _Result(scalars=[account]),
        _Result(scalars=[closing, opening]),
        _Result(
            rows=[
                (
                    "account-1",
                    50,
                    "deposit",
                    "EUR",
                    datetime(2026, 8, 10, tzinfo=UTC),
                )
            ]
        ),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._cash_reconciliation_issues()

    assert len(issues) == 1
    assert issues[0].id.startswith("cash-reconciliation-flow:")
    assert issues[0].evidence["opening_balance"] == "100"
    assert issues[0].evidence["expected_balance"] == "150"
    assert issues[0].evidence["snapshot_balance"] == "140"
    assert issues[0].evidence["transaction_count"] == 1


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
    assert [issue.category for issue in overview.issues] == [
        "failed_export",
        "unresolved_security",
    ]
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
        _Result(scalars=[]),
        _Result(rows=[("bunq", "account-1", 2, 10, 20)]),
        _Result(scalar=1),
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
    assert issues[2].evidence == {
        "affected_import_count": 1,
        "detail_limit": 20,
    }


@pytest.mark.asyncio
async def test_failed_legacy_export_is_visible_in_data_health() -> None:
    run = SimpleNamespace(
        id="export-1",
        exporter_type="wealthfolio",
        target_id="legacy",
        status="failed",
        started_at=datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
    )
    session = _Session(
        _Result(rows=[run]),
        _Result(rows=[]),
        _Result(scalar=0),
        _Result(scalars=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session),
        "tenant-a",
        permissions={"destinations:read", "destinations:write"},
    )._additional_issues()

    export_issue = next(
        issue for issue in issues if issue.category == "failed_export"
    )
    assert export_issue.action.key == "retry_export"
    assert export_issue.action.path.endswith("/wealthfolio/runs/export-1/retry")


@pytest.mark.asyncio
async def test_canonical_data_health_checks_expose_record_details() -> None:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    session = _Session(
        _Result(rows=[("tx-1", "Broker", "Koop zonder prijs", now, 7)]),
        _Result(
            rows=[
                (
                    "tx-2",
                    "Broker",
                    "Corporate action",
                    now,
                    "purchase",
                    1,
                    0,
                    0,
                    now,
                )
            ]
        ),
        _Result(rows=[("account-1", "Broker", -10)]),
        _Result(
            rows=[
                (
                    "account-1",
                    "Broker",
                    -75,
                    "EUR",
                    now.date(),
                    "Pensioeninleg",
                )
            ]
        ),
        _Result(rows=[("holding-1", "Broker", "VWCE", 12)]),
        _Result(rows=[("security-1", "Onbekende security", 8)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session),
        "tenant-a",
        permissions={
            "transactions:read",
            "accounts:read",
            "holdings:read",
        },
    )._canonical_data_issues()

    assert [issue.category for issue in issues] == [
        "incomplete_transaction",
        "zero_cost_transaction",
        "negative_balance",
        "unbalanced_transfer",
        "incomplete_holding",
        "incomplete_security_identity",
    ]
    assert issues[0].action.key == "view_transactions"
    assert issues[0].details == ["Broker · 2026-08-25 · Koop zonder prijs"]
    assert issues[0].impact_count == 7
    assert issues[0].evidence == {"total_count": 7, "detail_limit": 100}
    assert issues[3].impact_count == 1
    assert issues[4].action.key == "view_holdings"
    assert issues[4].impact_count == 12
    assert issues[4].evidence == {"total_count": 12, "detail_limit": 100}
    assert issues[5].impact_count == 8
    assert issues[5].evidence == {"total_count": 8, "detail_limit": 100}


@pytest.mark.asyncio
async def test_canonical_data_health_does_not_duplicate_incomplete_transfer():
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(
            rows=[
                (
                    "transfer-missing-amount",
                    "account-1",
                    "Broker",
                    None,
                    "EUR",
                    now.date(),
                    "Transfer out",
                )
            ]
        ),
        _Result(rows=[]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert [issue.category for issue in issues] == [
        "unbalanced_transfer",
        "incomplete_transaction",
    ]
    assert sum(
        "transfer-missing-amount" in issue.details[0] for issue in issues
    ) == 2


@pytest.mark.asyncio
async def test_canonical_data_health_projects_invalid_activity_order() -> None:
    occurred_at = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    booked_at = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    session = _Session(
        _Result(rows=[]),
        _Result(
            rows=[
                (
                    "tx-order",
                    "Broker",
                    "Trade",
                    occurred_at,
                    "purchase",
                    1,
                    10,
                    10,
                    booked_at,
                )
            ]
        ),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert len(issues) == 1
    assert issues[0].category == "invalid_activity_semantics"
    assert issues[0].blocking is True
    assert issues[0].affected_transaction_ids == ["tx-order"]


@pytest.mark.asyncio
async def test_canonical_data_health_projects_shared_activity_contract_findings():
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    session = _Session(
        _Result(rows=[]),
        _Result(
            rows=[
                (
                    "tx-contract",
                    "Broker",
                    "Trade",
                    now,
                    "purchase",
                    1,
                    10,
                    -10,
                    now,
                    None,
                    "EURO",
                    None,
                    None,
                    "",
                    "EUR",
                    "USD",
                    None,
                )
            ]
        ),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert [issue.category for issue in issues] == [
        "incomplete_transaction",
        "invalid_activity_semantics",
    ]
    assert issues[0].affected_transaction_ids == ["tx-contract"]
    assert issues[1].affected_transaction_ids == ["tx-contract"]


@pytest.mark.asyncio
async def test_canonical_data_health_deduplicates_repeated_activity_rows() -> None:
    """Overlapping source rows must not inflate one canonical finding."""
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    repeated_row = (
        "tx-repeat",
        "Broker",
        "Corporate action",
        now,
        "purchase",
        1,
        0,
        0,
        now,
    )
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[repeated_row, repeated_row]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert len(issues) == 1
    assert issues[0].category == "zero_cost_transaction"
    assert issues[0].impact_count == 1
    assert issues[0].affected_transaction_ids == ["tx-repeat"]


@pytest.mark.asyncio
async def test_canonical_data_health_uses_contract_window_counts() -> None:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    row = (
        "tx-contract-count",
        "Broker",
        "Trade",
        now,
        "purchase",
        1,
        10,
        10,
        now,
        None,
        "EURO",
        None,
        None,
        "",
        "EUR",
        "USD",
        None,
        0,
        0,
        1,
        3,
    )
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[row]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert [issue.category for issue in issues] == [
        "incomplete_transaction",
        "invalid_activity_semantics",
    ]
    assert issues[0].impact_count == 1
    assert issues[0].evidence == {"total_count": 1, "detail_limit": 100}
    assert issues[1].impact_count == 3
    assert issues[1].evidence == {"total_count": 3, "detail_limit": 100}


@pytest.mark.asyncio
async def test_canonical_data_health_projects_fee_and_cash_contracts() -> None:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    fee_row = (
        "tx-fee",
        "Broker",
        "Fee",
        now,
        "fee",
        None,
        None,
        -1,
        now,
        None,
        "EUR",
        0,
        "EURO",
        "fee-1",
        "EUR",
        None,
        None,
        0,
        0,
        1,
        2,
    )
    cash_row = (
        "tx-cash",
        "Broker",
        "Deposit",
        now,
        "deposit",
        None,
        None,
        None,
        now,
        None,
        "EUR",
        None,
        None,
        "cash-1",
        "EUR",
        None,
        None,
        0,
        0,
        1,
        0,
    )
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[fee_row, cash_row]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert [issue.category for issue in issues] == [
        "incomplete_transaction",
        "invalid_activity_semantics",
    ]
    assert issues[0].impact_count == 1
    assert issues[1].impact_count == 2
    assert issues[1].evidence == {"total_count": 2, "detail_limit": 100}


@pytest.mark.asyncio
async def test_canonical_projection_matches_shared_preflight_validator() -> None:
    """The canonical projection must preserve the shared validator finding."""
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    canonical = SimpleNamespace(
        id="tx-fee-chain",
        transaction_type="fee",
        amount=-1,
        currency_code="EUR",
        fee_amount=0,
        fee_currency_code="EUR",
        external_transaction_id="fee-chain-1",
        occurred_at=now,
        booked_at=now,
    )
    shared_findings = validate_activity_semantics(canonical)
    assert len(shared_findings) == 1
    assert shared_findings[0].category == "invalid_activity_semantics"

    row = (
        "tx-fee-chain",
        "Broker",
        "Fee",
        now,
        "fee",
        None,
        None,
        -1,
        now,
        None,
        "EUR",
        0,
        "EUR",
        "fee-chain-1",
        "EUR",
        None,
        None,
        0,
        0,
        0,
        1,
    )
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[row]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )
    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert len(issues) == 1
    assert issues[0].category == shared_findings[0].category
    assert shared_findings[0].message in issues[0].details[0]


@pytest.mark.asyncio
async def test_canonical_projection_validates_quantity_events_with_metadata() -> None:
    """Canonical preflight must include persisted quantity-event metadata."""
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    canonical = SimpleNamespace(
        id="tx-split-chain",
        transaction_type="corporate_action",
        amount=0,
        currency_code="EUR",
        external_transaction_id="split-chain-1",
        provider_metadata_contract={"event": "stock split"},
        occurred_at=now,
        booked_at=now,
    )
    shared_findings = validate_activity_semantics(canonical)
    assert len(shared_findings) == 1
    assert shared_findings[0].category == "invalid_activity_semantics"

    row = (
        "tx-split-chain",
        "Broker",
        "Stock split",
        now,
        "corporate_action",
        None,
        None,
        0,
        now,
        "security-1",
        "EUR",
        None,
        None,
        "split-chain-1",
        "EUR",
        "EUR",
        None,
        0,
        0,
        0,
        1,
        {"event": "stock split"},
    )
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[row]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )
    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._canonical_data_issues()

    assert len(issues) == 1
    assert issues[0].category == shared_findings[0].category
    assert shared_findings[0].message in issues[0].details[0]


@pytest.mark.asyncio
async def test_wealthfolio_preflight_exposes_destination_quality_issues() -> (
    None
):
    session = _Session(
        _Result(rows=[("ACME", "quote rejected")]),
        _Result(rows=[("Broker", datetime(2026, 8, 25).date(), -10)]),
        _Result(rows=[]),
        _Result(rows=[("holding-1", "Broker", "ACME", 4)]),
        _Result(rows=[("holding-2", "Broker", "VWCE", 6)]),
        _Result(rows=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session),
        "tenant-a",
        permissions={"enrichment:write", "transactions:read"},
    )._wealthfolio_preflight_issues()

    assert [issue.category for issue in issues] == [
        "quote_sync_failure",
        "negative_valuation",
        "incomplete_valuation",
        "incomplete_cost_basis",
    ]
    assert issues[0].action.key == "refresh_quotes"
    assert issues[1].action.key == "view_transactions"
    assert issues[2].impact_count == 4
    assert issues[2].evidence == {"total_count": 4, "detail_limit": 1000}
    assert issues[3].impact_count == 6
    assert issues[3].evidence == {"total_count": 6, "detail_limit": 1000}


@pytest.mark.asyncio
async def test_wealthfolio_preflight_flags_unverified_cost_basis() -> None:
    session = _Session(
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[("holding-3", "Broker", "VWCE", 2)]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._wealthfolio_preflight_issues()

    assert len(issues) == 1
    assert issues[0].category == "incomplete_cost_basis"
    assert issues[0].severity == "warning"
    assert issues[0].evidence == {
        "total_count": 2,
        "detail_limit": 1000,
        "basis_present": True,
        "basis_source": "unverified",
    }


@pytest.mark.asyncio
async def test_additional_health_issues_ignore_extra_account_projection_values() -> (
    None
):
    session = _Session(
        _Result(rows=[("bunq", "account-1", 2, 10, 20, "new-column")]),
        _Result(scalars=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._additional_issues()

    assert [issue.category for issue in issues] == [
        "duplicate_accounts",
        "balance_conflict",
    ]


@pytest.mark.asyncio
async def test_additional_health_issues_skip_short_account_projection_rows() -> (
    None
):
    session = _Session(
        _Result(rows=[("bunq", "account-1", 2, 10)]),
        _Result(scalars=[]),
    )

    issues = await DataHealthService(
        cast("AsyncSession", session), "tenant-a"
    )._additional_issues()

    assert issues == []


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
    assert issues[0].action.path.endswith("connection-bunq/start")
    assert issues[0].evidence == {"total_count": 3, "detail_limit": 1}


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
