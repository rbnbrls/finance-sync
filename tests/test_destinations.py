"""Focused tests for destination wizard safety contracts."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from finance_sync.api.v1.destinations import (
    _JUPYTER_PERMISSIONS,
    TargetCreate,
    _actual_account_mapping_preview,
    _jupyter_notebook,
    _response,
    _safe_url,
    _validate_body,
)
from finance_sync.exporter.actual_budget.models import (
    ActualBudgetAccountMapping,
    ExportDelivery,
)
from finance_sync.exporter.wealthfolio.models import (
    WealthfolioAccountMapping,
    WealthfolioDelivery,
)
from finance_sync.models.export_target import ExportTarget
from finance_sync.models.sync_schedule import SyncSchedule
from finance_sync.services.visibility import ReadScope


def test_private_http_and_https_are_accepted() -> None:
    assert _safe_url("http://192.168.1.8:5006/") == "http://192.168.1.8:5006"
    assert (
        _safe_url("https://budget.example.test")
        == "https://budget.example.test"
    )


def test_public_http_is_rejected() -> None:
    with pytest.raises(HTTPException, match="local or private"):
        _safe_url("http://example.test")


def test_secret_cannot_be_saved_in_visible_configuration() -> None:
    body = TargetCreate(
        target_type="wealthfolio",
        display_name="Portfolio",
        configuration={
            "server_url": "https://wealth.example.test",
            "password": "no",
        },
    )
    with pytest.raises(HTTPException, match="credentials"):
        _validate_body(body, body.target_type)


@pytest.mark.parametrize("field", ["api_key", "credential", "authorization"])
def test_all_credential_like_configuration_is_rejected(field: str) -> None:
    body = TargetCreate(
        target_type="wealthfolio",
        display_name="Portfolio",
        configuration={
            "server_url": "https://wealth.example.test",
            field: "not-here",
        },
    )
    with pytest.raises(HTTPException, match="credentials"):
        _validate_body(body, body.target_type)


def test_jupyter_default_datasets_are_read_only() -> None:
    body = TargetCreate(target_type="jupyter", display_name="Notebook")
    assert body.datasets == [
        "accounts",
        "transactions",
        "holdings",
        "securities",
        "prices",
    ]


def test_jupyter_scope_excludes_write_permissions() -> None:
    assert all(part.endswith(":read") for part in _JUPYTER_PERMISSIONS.split())


def test_jupyter_starter_declares_a_versioned_read_only_contract() -> None:
    notebook = _jupyter_notebook()
    assert "consumer contract v1" in notebook
    for dataset in (
        "accounts",
        "transactions",
        "holdings",
        "securities",
        "prices",
    ):
        assert f"read_dataset('{dataset}')" in notebook
    assert "FINANCE_SYNC_JUPYTER_TOKEN" in notebook
    assert "'X-API-Key': TOKEN" in notebook
    assert "Authorization" not in notebook
    assert "POST" not in notebook
    compile(notebook, "finance-sync-datalake-starter.py", "exec")


def test_actual_budget_preview_describes_existing_and_new_accounts() -> None:
    preview = _actual_account_mapping_preview(
        [("checking", "Checking"), ("savings", "Savings")],
        [{"id": "ab-checking", "name": "Checking", "offbudget": True}],
        default_off_budget=False,
    )
    assert preview == [
        {
            "id": "checking",
            "name": "Checking",
            "action": "use_existing",
            "actual_budget_account_id": "ab-checking",
            "actual_budget_account_name": "Checking",
            "off_budget": True,
        },
        {
            "id": "savings",
            "name": "Savings",
            "action": "create_on_first_sync",
            "actual_budget_account_id": None,
            "actual_budget_account_name": None,
            "off_budget": False,
        },
    ]


def test_consumer_key_scope_limits_visible_accounts() -> None:
    allowed = ReadScope.for_api_key("tenant-1", account_scope=["allowed"])
    allowed_account = type(
        "Account",
        (),
        {
            "id": "allowed",
            "tenant_id": "tenant-1",
            "owner_user_id": "owner",
        },
    )()
    excluded_account = type(
        "Account",
        (),
        {
            "id": "excluded",
            "tenant_id": "tenant-1",
            "owner_user_id": "owner",
        },
    )()
    assert allowed.is_visible(allowed_account)  # type: ignore[arg-type]
    assert not allowed.is_visible(excluded_account)  # type: ignore[arg-type]
    private_allowed = type(
        "Account",
        (),
        {
            "id": "allowed",
            "tenant_id": "tenant-1",
            "owner_user_id": "owner",
        },
    )()
    assert allowed.is_visible(private_allowed)  # type: ignore[arg-type]


def test_destination_response_exposes_only_safe_schedule_metadata() -> None:
    now = datetime.now(UTC)
    target = ExportTarget(
        id="target-1",
        tenant_id="tenant-1",
        target_type="wealthfolio",
        display_name="Portfolio",
        status="active",
        version=1,
        configuration={"server_url": "https://wealth.example.test"},
        selected_account_ids=[],
        datasets=["accounts"],
        created_at=now,
        updated_at=now,
    )
    schedule = SyncSchedule(
        id="schedule-1",
        tenant_id="tenant-1",
        scope="export",
        target_id="wealthfolio:target-1",
        enabled=True,
        schedule={},
        timezone="Europe/Amsterdam",
        version=1,
        next_run_at=now,
        last_run_at=now,
        last_run_status="completed",
        last_run_error=None,
    )
    response = _response(target, schedule)
    assert response.next_run_at == now
    assert response.last_run_status == "completed"
    assert "secret" not in response.configuration


@pytest.mark.parametrize(
    "model",
    [
        ActualBudgetAccountMapping,
        ExportDelivery,
        WealthfolioAccountMapping,
        WealthfolioDelivery,
    ],
)
def test_delivery_state_is_scoped_by_destination(model: type[object]) -> None:
    table = model.__table__  # type: ignore[attr-defined]
    assert "target_id" in table.c
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tenant_id", "target_id", "account_id") in unique_columns
