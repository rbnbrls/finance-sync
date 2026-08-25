"""PostgreSQL coverage for the phase-4 control-plane data-quality feed."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    Tenant,
    Transaction,
    UnresolvedSecurity,
)
from finance_sync.services.control_plane import ControlPlaneService

pytestmark = pytest.mark.integration


async def test_control_plane_data_quality_is_tenant_scoped_and_actionable(
    session,
) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    tenant_a = Tenant(slug="phase4-a", name="Phase 4 A")
    tenant_b = Tenant(slug="phase4-b", name="Phase 4 B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    credential = Credential(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        encrypted_payload=b"ciphertext",
        nonce=b"nonce",
    )
    account = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        connection_id=str(credential.id),
        external_account_id="account-a",
        name="Checking",
        account_type="checking",
        currency_code="EUR",
    )
    candidate = Security(
        id=uuid4(),
        ticker="ACME",
        name="Acme Corporation",
        security_type="stock",
        currency_code="EUR",
    )
    unresolved = UnresolvedSecurity(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        external_security_id="provider-security-a",
        raw_ticker="ACME",
        raw_name="Acme Corporation",
    )
    transaction = Transaction(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        connection_id=str(credential.id),
        external_transaction_id="transaction-a",
        account_id=str(account.id),
        amount=Decimal(10),
        currency_code="EUR",
        occurred_at=now - timedelta(hours=1),
        transaction_type="purchase",
        status="booked",
    )
    holding = Holding(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        account_id=str(account.id),
        security_id=str(candidate.id),
        observed_at=now - timedelta(hours=1),
        quantity=Decimal(1),
        currency_code="EUR",
        source="provider_sync",
    )
    reconciliation_run = ReconciliationRun(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        status="completed",
        started_at=now - timedelta(minutes=5),
        completed_at=now - timedelta(minutes=1),
        finding_count=1,
    )
    reconciliation_result = ReconciliationResult(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        run_id=str(reconciliation_run.id),
        kind="missing_transaction",
        severity="warning",
        account_id=str(account.id),
        provider_key="bunq",
        transaction_id_a=str(transaction.id),
        description="Transaction ontbreekt in een tweede bron",
    )
    target = ExportTarget(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        target_type="wealthfolio",
        display_name="Portfolio",
        status="active",
        selected_account_ids=[str(account.id)],
        datasets=["transactions"],
        configuration={},
    )
    export_run = ExportRun(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        target_id=str(target.id),
        exporter_type="wealthfolio",
        status="failed",
        started_at=now - timedelta(minutes=3),
        completed_at=now - timedelta(minutes=2),
        error_message="provider unavailable",
    )
    session.add_all([credential, account, candidate])
    await session.flush()
    session.add_all(
        [unresolved, transaction, holding, reconciliation_run, target]
    )
    await session.flush()
    reconciliation_result.run_id = str(reconciliation_run.id)
    reconciliation_result.account_id = str(account.id)
    reconciliation_result.transaction_id_a = str(transaction.id)
    export_run.target_id = str(target.id)
    # Tenant B has an equally named unresolved record but must not appear.
    session.add(
        UnresolvedSecurity(
            tenant_id=str(tenant_b.id),
            provider_key="bunq",
            external_security_id="provider-security-b",
        )
    )
    session.add_all([reconciliation_result, export_run])
    await session.commit()

    overview = await ControlPlaneService(
        session,
        str(tenant_a.id),
        permissions={"*:*"},
        now=now,
    ).get_overview()

    security_issue = next(
        issue
        for issue in overview.issues
        if issue.category == "security_mapping"
    )
    assert security_issue.provider == "bunq"
    assert security_issue.impact_count >= 2
    assert security_issue.candidate_securities[0]["ticker"] == "ACME"
    assert security_issue.action.path == "/api/v1/securities/map"
    assert any(issue.category == "data_quality" for issue in overview.issues)
    export_issue = next(
        issue for issue in overview.issues if issue.category == "export"
    )
    assert export_issue.action.key == "retry_export"
    assert overview.as_of == now - timedelta(minutes=1)
