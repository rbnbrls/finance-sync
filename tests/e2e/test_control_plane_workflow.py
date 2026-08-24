"""E2E control-plane recovery contract.

This test deliberately seeds the operational failure states that the dashboard
must expose and reads them through the authenticated API, using the same real
PostgreSQL/Redis-backed application harness as the other E2E tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from finance_sync.exporter.models import ExportRun
from finance_sync.models import (
    Account,
    Credential,
    ExportTarget,
    Holding,
    ReconciliationResult,
    ReconciliationRun,
    Security,
    SyncRun,
    SyncSchedule,
    Transaction,
    UnresolvedSecurity,
)
from tests.e2e.destinations_helpers import (
    dest_client,
    seeded_destination_tenant,
)

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.e2e

client = dest_client
seeded_tenant = seeded_destination_tenant


def _route_matches(route_path: str, action_path: str) -> bool:
    """Return whether an action URL is covered by a registered API route."""
    route_parts = route_path.strip("/").split("/")
    action_parts = action_path.split("?", 1)[0].strip("/").split("/")
    if len(route_parts) != len(action_parts):
        return False
    return all(
        route_part.startswith("{") or route_part == action_part
        for route_part, action_part in zip(
            route_parts, action_parts, strict=True
        )
    )


def _assert_action_routes(
    app: object, actions: list[dict[str, object]]
) -> None:
    """Ensure every control-plane action points to a real API operation."""
    routes = getattr(app, "routes", [])
    for item in actions:
        method = item["method"]
        path = item["path"]
        assert isinstance(method, str)
        assert isinstance(path, str)
        assert any(
            method in (getattr(route, "methods", set()) or set())
            and _route_matches(getattr(route, "path", ""), path)
            for route in routes
        ), f"Geen API-route voor control-planeactie {method} {path}"


async def test_control_plane_recovery_workflow_is_exposed_and_scoped(
    client: httpx.AsyncClient,
    e2e_app: object,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_tenant: dict[str, str],
) -> None:
    """The dashboard API exposes all recovery pivots without leaking data."""
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    tenant_id = seeded_tenant["tenant_id"]

    async with session_factory() as session:
        credential = Credential(
            id=uuid4(),
            tenant_id=tenant_id,
            provider_key="bunq",
            encrypted_payload=b"ciphertext",
            nonce=b"nonce",
            last_error="provider unavailable",
        )
        account = Account(
            id=uuid4(),
            tenant_id=tenant_id,
            provider_key="bunq",
            connection_id=str(credential.id),
            external_account_id="e2e-account",
            name="E2E Checking",
            account_type="checking",
            currency_code="EUR",
        )
        security = Security(
            id=uuid4(),
            ticker="E2E",
            name="E2E Security",
            security_type="stock",
            currency_code="EUR",
        )
        unresolved = UnresolvedSecurity(
            id=uuid4(),
            tenant_id=tenant_id,
            provider_key="bunq",
            external_security_id="provider-e2e-security",
            raw_ticker="E2E",
            raw_name="E2E Security",
        )
        transaction = Transaction(
            id=uuid4(),
            tenant_id=tenant_id,
            provider_key="bunq",
            connection_id=str(credential.id),
            external_transaction_id="e2e-transaction",
            account_id=str(account.id),
            amount=Decimal("10.00"),
            currency_code="EUR",
            occurred_at=now - timedelta(hours=1),
            transaction_type="purchase",
            status="booked",
        )
        holding = Holding(
            id=uuid4(),
            tenant_id=tenant_id,
            account_id=str(account.id),
            security_id=str(security.id),
            observed_at=now - timedelta(hours=1),
            quantity=1,
            currency_code="EUR",
            source="provider_sync",
        )
        sync_run = SyncRun(
            id=uuid4(),
            connection_id=str(credential.id),
            connector="bunq",
            status="failed",
            started_at=now - timedelta(minutes=10),
            completed_at=now - timedelta(minutes=9),
            error_message="provider unavailable",
            error_category="provider_unavailable",
        )
        target = ExportTarget(
            id=uuid4(),
            tenant_id=tenant_id,
            target_type="wealthfolio",
            display_name="E2E Portfolio",
            status="active",
            selected_account_ids=[str(account.id)],
            datasets=["transactions"],
            configuration={},
        )
        export_run = ExportRun(
            id=uuid4(),
            tenant_id=tenant_id,
            target_id=str(target.id),
            exporter_type="wealthfolio",
            status="failed",
            started_at=now - timedelta(minutes=8),
            completed_at=now - timedelta(minutes=7),
            error_message="provider unavailable",
        )
        reconciliation_run = ReconciliationRun(
            id=uuid4(),
            tenant_id=tenant_id,
            status="completed",
            started_at=now - timedelta(minutes=6),
            completed_at=now - timedelta(minutes=5),
            finding_count=1,
        )
        reconciliation_result = ReconciliationResult(
            id=uuid4(),
            tenant_id=tenant_id,
            run_id=str(reconciliation_run.id),
            kind="missing_transaction",
            severity="warning",
            account_id=str(account.id),
            provider_key="bunq",
            transaction_id_a=str(transaction.id),
            description="Transaction ontbreekt in tweede bron",
        )
        session.add_all([credential, account, security])
        await session.flush()
        session.add_all(
            [
                unresolved,
                transaction,
                holding,
                sync_run,
                target,
                export_run,
                reconciliation_run,
            ]
        )
        await session.flush()
        schedule = SyncSchedule(
            tenant_id=tenant_id,
            scope="export",
            target_id=f"wealthfolio:{target.id}",
            enabled=True,
            schedule={"frequency": "daily", "time": "07:00"},
            timezone="UTC",
        )
        session.add(schedule)
        await session.flush()
        target.schedule_id = str(schedule.id)
        session.add(reconciliation_result)
        await session.commit()

    response = await client.get(
        "/api/v1/control-plane/overview", headers=seeded_tenant["headers"]
    )
    assert response.status_code == 200, response.text
    overview = response.json()
    assert overview["status"] == "sync_failed"
    assert overview["destinations"][0]["failed_export_count"] == 1
    assert {action["key"] for action in overview["syncs"][0]["actions"]} >= {
        "view_sync_run",
        "retry_sync",
    }
    assert {
        action["key"] for action in overview["destinations"][0]["actions"]
    } >= {
        "test_destination",
        "run_export",
        "retry_export",
    }
    security_issue = next(
        issue
        for issue in overview["issues"]
        if issue["category"] == "security_mapping"
    )
    assert security_issue["provider"] == "bunq"
    assert security_issue["impact_count"] >= 2
    assert security_issue["candidate_securities"][0]["ticker"] == "E2E"
    assert any(
        issue["category"] == "data_quality" for issue in overview["issues"]
    )

    actions = [
        action
        for section in (
            overview["connections"],
            overview["syncs"],
            overview["issues"],
            overview["destinations"],
        )
        for item in section
        for action in (
            item.get("actions", [])
            if isinstance(item, dict)
            else [item.get("action")]
        )
        if isinstance(action, dict)
    ]
    _assert_action_routes(e2e_app, actions)

    retry = await client.post(
        f"/api/v1/destinations/{target.id}/retry",
        headers=seeded_tenant["headers"],
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "skipped"

    dashboard = await client.get("/")
    assert dashboard.status_code == 200
    assert "control-plane" in dashboard.text

    quality = await client.get(
        "/api/v1/control-plane/data-quality", headers=seeded_tenant["headers"]
    )
    assert quality.status_code == 200, quality.text
    assert {"status", "findings_total", "issues"} <= quality.json().keys()

    analytics = await client.get(
        "/api/v1/analytics/overview", headers=seeded_tenant["headers"]
    )
    assert analytics.status_code == 200, analytics.text
    analytics_body = analytics.json()
    assert {
        "subscriptions",
        "market_intelligence",
        "ai_summary",
        "meta",
    } <= analytics_body.keys()
    assert analytics_body["subscriptions"]["coverage"]["items"] == 0
