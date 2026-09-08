"""PostgreSQL coverage for the phase-4 control-plane data-quality feed."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.models import (
    WealthfolioAccountMapping,
    WealthfolioDelivery,
)
from finance_sync.models import (
    Account,
    Balance,
    Credential,
    ExportTarget,
    Holding,
    ReconciliationResult,
    ReconciliationRun,
    Security,
    SyncCursor,
    SyncRun,
    TaxLot,
    Tenant,
    Transaction,
    UnresolvedSecurity,
)
from finance_sync.services.control_plane import ControlPlaneService
from finance_sync.services.data_health import DataHealthService

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
    # The issue is one unresolved provider identity, with both canonical
    # transaction and holding references counted as affected records.
    assert security_issue.impact_count == 2
    assert security_issue.candidate_securities[0]["ticker"] == "ACME"
    assert security_issue.action.path == "/api/v1/securities/map"
    assert any(issue.category == "data_quality" for issue in overview.issues)
    export_issue = next(
        issue for issue in overview.issues if issue.category == "export"
    )
    assert export_issue.action.key == "retry_export"
    assert overview.as_of == now - timedelta(minutes=1)


async def test_data_health_identity_checks_are_tenant_and_connection_scoped(
    session,
) -> None:
    """Identity checks must not collapse or leak records across tenants."""
    tenant_a = Tenant(slug="phase4-health-a", name="Health A")
    tenant_b = Tenant(slug="phase4-health-b", name="Health B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    credential_a1 = Credential(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        encrypted_payload=b"a1",
        nonce=b"n1",
    )
    credential_a2 = Credential(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        encrypted_payload=b"a2",
        nonce=b"n2",
    )
    credential_b = Credential(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        encrypted_payload=b"b1",
        nonce=b"n3",
    )
    account_a1 = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        connection_id=str(credential_a1.id),
        external_account_id="provider-account",
        name="Trading212",
        account_type="brokerage",
        currency_code="EUR",
        current_balance=100,
    )
    account_a2 = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        connection_id=str(credential_a2.id),
        external_account_id="provider-account",
        name="Trading212",
        account_type="brokerage",
        currency_code="EUR",
        current_balance=100,
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        connection_id=str(credential_b.id),
        external_account_id="provider-account",
        name="Trading212",
        account_type="brokerage",
        currency_code="EUR",
    )
    session.add_all(
        [
            credential_a1,
            credential_a2,
            credential_b,
            account_a1,
            account_a2,
            account_b,
        ]
    )
    await session.flush()

    transaction_a1 = Transaction(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        connection_id=str(credential_a1.id),
        external_transaction_id="provider-transaction",
        account_id=str(account_a1.id),
        amount=10,
        currency_code="EUR",
        occurred_at=datetime(2026, 8, 24, tzinfo=UTC),
        transaction_type="deposit",
        status="booked",
    )
    transaction_a2 = Transaction(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        connection_id=str(credential_a2.id),
        external_transaction_id="provider-transaction",
        account_id=str(account_a2.id),
        amount=10,
        currency_code="EUR",
        occurred_at=datetime(2026, 8, 24, tzinfo=UTC),
        transaction_type="deposit",
        status="booked",
    )
    session.add_all([transaction_a1, transaction_a2])
    await session.commit()

    service = DataHealthService(session, str(tenant_a.id))
    account_issues = await service._account_identity_issues()
    transaction_issues = await service._transaction_identity_issues()

    assert len(account_issues) == 1
    assert set(account_issues[0].account_ids) == {
        str(account_a1.id),
        str(account_a2.id),
    }
    assert len(transaction_issues) == 1
    assert transaction_issues[0].impact_count == 2


async def test_data_health_metadata_identity_is_tenant_scoped(session) -> None:
    """Stable provider metadata must not collapse accounts across tenants."""
    tenant_a = Tenant(slug="phase4-metadata-a", name="Metadata A")
    tenant_b = Tenant(slug="phase4-metadata-b", name="Metadata B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    account_a1 = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        external_account_id="provider-a1",
        name="Checking A",
        account_type="checking",
        currency_code="EUR",
        provider_metadata={"iban": "NL00BANK0123456789"},
    )
    account_a2 = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        external_account_id="provider-a2",
        name="Savings A",
        account_type="savings",
        currency_code="EUR",
        provider_metadata={"iban": "NL00 BANK 0123456789"},
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="bunq",
        external_account_id="provider-b",
        name="Checking B",
        account_type="checking",
        currency_code="EUR",
        provider_metadata={"iban": "NL00BANK0123456789"},
    )
    session.add_all([account_a1, account_a2, account_b])
    await session.commit()

    issues = await DataHealthService(
        session, str(tenant_a.id), permissions={"accounts:read"}
    )._account_metadata_identity_issues()

    assert len(issues) == 1
    assert issues[0].category == "account_identity_conflict"
    assert issues[0].impact_count == 2
    assert issues[0].account_ids == sorted(
        [str(account_a1.id), str(account_a2.id)]
    )
    assert "provider-b" not in issues[0].account_ids
    assert issues[0].evidence["metadata_key"] == "iban"


async def test_data_health_metadata_alias_identity_is_tenant_scoped(
    session,
) -> None:
    """Provider-specific account aliases stay normalized inside one tenant."""
    tenant_a = Tenant(slug="phase4-metadata-alias-a", name="Metadata Alias A")
    tenant_b = Tenant(slug="phase4-metadata-alias-b", name="Metadata Alias B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    account_a1 = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        external_account_id="provider-a1",
        name="Checking A",
        account_type="checking",
        currency_code="EUR",
        provider_metadata={"monetaryAccountId": "1000001"},
    )
    account_a2 = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        external_account_id="provider-a2",
        name="Savings A",
        account_type="savings",
        currency_code="EUR",
        provider_metadata={"monetary_account_id": "1000001"},
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="bunq",
        external_account_id="provider-b",
        name="Checking B",
        account_type="checking",
        currency_code="EUR",
        provider_metadata={"monetaryAccountId": "1000001"},
    )
    session.add_all([account_a1, account_a2, account_b])
    await session.commit()

    issues = await DataHealthService(
        session, str(tenant_a.id), permissions={"accounts:read"}
    )._account_metadata_identity_issues()

    assert len(issues) == 1
    assert issues[0].evidence["metadata_key"] == "monetary_account_id"
    assert issues[0].account_ids == sorted(
        [str(account_a1.id), str(account_a2.id)]
    )
    assert str(account_b.id) not in issues[0].account_ids


async def test_data_health_sync_integrity_is_tenant_scoped(session) -> None:
    """Sync cursor/run findings must only include the requested tenant."""
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    tenant_a = Tenant(slug="phase4-sync-a", name="Sync A")
    tenant_b = Tenant(slug="phase4-sync-b", name="Sync B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    credential_a = Credential(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        encrypted_payload=b"a",
        nonce=b"a",
    )
    credential_b = Credential(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        encrypted_payload=b"b",
        nonce=b"b",
    )
    session.add_all([credential_a, credential_b])
    await session.flush()

    session.add_all(
        [
            SyncCursor(
                tenant_id=str(tenant_a.id),
                connector="wrong-provider",
                connection_id=credential_a.id,
                resource="transactions",
                cursor=now,
            ),
            SyncCursor(
                tenant_id=str(tenant_a.id),
                connector="trading212",
                connection_id=credential_b.id,
                resource="transactions",
                cursor=now,
            ),
            SyncRun(
                id=uuid4(),
                connector="trading212",
                connection_id=credential_a.id,
                resource="transactions",
                status="completed",
                started_at=now - timedelta(minutes=2),
                completed_at=now - timedelta(minutes=1),
                report={"failed": 1, "skipped": 0},
            ),
            SyncRun(
                id=uuid4(),
                connector="trading212",
                connection_id=credential_b.id,
                resource="transactions",
                status="completed",
                started_at=now - timedelta(minutes=2),
                completed_at=now - timedelta(minutes=1),
                report={"failed": 1, "skipped": 0},
            ),
        ]
    )
    await session.commit()

    issues = await DataHealthService(
        session, str(tenant_a.id)
    )._sync_integrity_issues()

    cursor_issue = next(
        issue for issue in issues if issue.source == "sync_cursor"
    )
    run_issue = next(issue for issue in issues if issue.source == "sync_runs")
    assert cursor_issue.impact_count == 2
    assert run_issue.impact_count == 1


async def test_data_health_cash_and_quantity_checks_are_tenant_scoped(
    session,
) -> None:
    """Cash and holding aggregations must ignore another tenant's data."""
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    tenant_a = Tenant(slug="phase4-portfolio-a", name="Portfolio A")
    tenant_b = Tenant(slug="phase4-portfolio-b", name="Portfolio B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    account_a = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        external_account_id="account-a",
        name="Portfolio A",
        account_type="brokerage",
        currency_code="EUR",
        current_balance=100,
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        external_account_id="account-b",
        name="Portfolio B",
        account_type="brokerage",
        currency_code="EUR",
        current_balance=200,
    )
    security_a = Security(
        id=uuid4(),
        ticker="AAA",
        name="Asset A",
        security_type="stock",
        currency_code="EUR",
    )
    security_b = Security(
        id=uuid4(),
        ticker="BBB",
        name="Asset B",
        security_type="stock",
        currency_code="EUR",
    )
    session.add_all([account_a, account_b, security_a, security_b])
    await session.flush()

    session.add_all(
        [
            Transaction(
                id=uuid4(),
                tenant_id=str(tenant_a.id),
                provider_key="trading212",
                external_transaction_id="purchase-a",
                account_id=str(account_a.id),
                security_id=str(security_a.id),
                amount=300,
                currency_code="EUR",
                quantity=3,
                occurred_at=now - timedelta(days=1),
                transaction_type="purchase",
                status="booked",
            ),
            Transaction(
                id=uuid4(),
                tenant_id=str(tenant_b.id),
                provider_key="trading212",
                external_transaction_id="purchase-b",
                account_id=str(account_b.id),
                security_id=str(security_b.id),
                amount=400,
                currency_code="EUR",
                quantity=4,
                occurred_at=now - timedelta(days=1),
                transaction_type="purchase",
                status="booked",
            ),
            Holding(
                id=uuid4(),
                tenant_id=str(tenant_a.id),
                account_id=str(account_a.id),
                security_id=str(security_a.id),
                observed_at=now,
                quantity=4,
                currency_code="EUR",
                source="provider_sync",
            ),
            Holding(
                id=uuid4(),
                tenant_id=str(tenant_b.id),
                account_id=str(account_b.id),
                security_id=str(security_b.id),
                observed_at=now,
                quantity=5,
                currency_code="EUR",
                source="provider_sync",
            ),
            Balance(
                id=uuid4(),
                tenant_id=str(tenant_a.id),
                account_id=str(account_a.id),
                observed_at=now,
                balance_kind="current",
                amount=101,
                currency_code="EUR",
                source="provider_sync",
            ),
            Balance(
                id=uuid4(),
                tenant_id=str(tenant_b.id),
                account_id=str(account_b.id),
                observed_at=now,
                balance_kind="current",
                amount=201,
                currency_code="EUR",
                source="provider_sync",
            ),
        ]
    )
    await session.commit()

    service = DataHealthService(session, str(tenant_a.id))
    quantity_issues = await service._portfolio_quantity_issues()
    cash_issues = await service._cash_reconciliation_issues()

    assert len(quantity_issues) == 1
    assert quantity_issues[0].account_ids == [str(account_a.id)]
    assert len(cash_issues) == 1
    assert cash_issues[0].account_ids == [str(account_a.id)]


async def test_data_health_tax_lot_cost_basis_is_tenant_scoped(session) -> None:
    """Invalid cost basis is reported only for the requested tenant."""
    tenant_a = Tenant(slug="phase4-lot-a", name="Lot A")
    tenant_b = Tenant(slug="phase4-lot-b", name="Lot B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    account_a = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        external_account_id="account-a",
        name="Portfolio A",
        account_type="brokerage",
        currency_code="EUR",
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        external_account_id="account-b",
        name="Portfolio B",
        account_type="brokerage",
        currency_code="EUR",
    )
    security_a = Security(
        id=uuid4(),
        isin="US0378331005",
        ticker="ACME",
        name="Acme",
        security_type="stock",
        currency_code="EUR",
    )
    security_b = Security(
        id=uuid4(),
        isin="US5949181045",
        ticker="BETA",
        name="Beta",
        security_type="stock",
        currency_code="EUR",
    )
    security_c = Security(
        id=uuid4(),
        isin="US0231351067",
        ticker="GAMMA",
        name="Gamma",
        security_type="stock",
        currency_code="EUR",
    )
    session.add_all([account_a, account_b, security_a, security_b, security_c])
    await session.flush()

    purchase_a = Transaction(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        external_transaction_id="purchase-a",
        account_id=str(account_a.id),
        security_id=str(security_a.id),
        amount=100,
        currency_code="EUR",
        quantity=10,
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
        transaction_type="purchase",
        status="booked",
    )
    purchase_b = Transaction(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        external_transaction_id="purchase-b",
        account_id=str(account_b.id),
        security_id=str(security_b.id),
        amount=100,
        currency_code="EUR",
        quantity=10,
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
        transaction_type="purchase",
        status="booked",
    )
    sale_without_lot = Transaction(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        external_transaction_id="sale-without-lot",
        account_id=str(account_a.id),
        security_id=str(security_c.id),
        amount=30,
        currency_code="EUR",
        quantity=3,
        occurred_at=datetime(2026, 8, 21, tzinfo=UTC),
        transaction_type="sale",
        status="booked",
    )
    session.add_all([purchase_a, purchase_b, sale_without_lot])
    await session.flush()
    session.add_all(
        [
            TaxLot(
                id=uuid4(),
                tenant_id=str(tenant_a.id),
                account_id=str(account_a.id),
                security_id=str(security_a.id),
                purchase_transaction_id=str(purchase_a.id),
                quantity=10,
                remaining_quantity=10,
                cost_basis_total=100,
                cost_basis_per_unit=9,
                currency_code="EUR",
                acquired_at=datetime(2026, 8, 20, tzinfo=UTC),
            ),
            TaxLot(
                id=uuid4(),
                tenant_id=str(tenant_b.id),
                account_id=str(account_b.id),
                security_id=str(security_b.id),
                purchase_transaction_id=str(purchase_b.id),
                quantity=10,
                remaining_quantity=10,
                cost_basis_total=100,
                cost_basis_per_unit=10,
                currency_code="EUR",
                acquired_at=datetime(2026, 8, 20, tzinfo=UTC),
            ),
        ]
    )
    await session.commit()

    issues = await DataHealthService(
        session, str(tenant_a.id), permissions={"holdings:read"}
    )._tax_lot_integrity_issues()

    assert len(issues) == 1
    assert issues[0].blocking is True
    assert issues[0].account_ids == [str(account_a.id)]
    assert issues[0].evidence["cost_basis_error_count"] == 1
    assert issues[0].evidence["unbacked_sale_group_count"] == 1


async def test_data_health_destination_parity_is_tenant_scoped(session) -> None:
    """Persisted destination health and mappings never cross tenants."""
    tenant_a = Tenant(slug="phase4-destination-a", name="Destination A")
    tenant_b = Tenant(slug="phase4-destination-b", name="Destination B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    account_a = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        external_account_id="account-a",
        name="Portfolio A",
        account_type="brokerage",
        currency_code="EUR",
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        external_account_id="account-b",
        name="Portfolio B",
        account_type="brokerage",
        currency_code="EUR",
    )
    target_a = ExportTarget(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        target_type="wealthfolio",
        display_name="Wealthfolio A",
        status="active",
        last_health_status="unauthorized",
        last_health_error="authentication rejected",
        configuration={},
    )
    target_b = ExportTarget(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        target_type="wealthfolio",
        display_name="Wealthfolio B",
        status="active",
        last_health_status="failed",
        last_health_error="should not leak",
        configuration={},
    )
    session.add_all([account_a, account_b, target_a, target_b])
    await session.flush()
    mapping_a = WealthfolioAccountMapping(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        target_id=str(target_a.id),
        account_id=str(account_a.id),
        wf_account_name="Portfolio A",
        wf_account_id=None,
        provider_account_id=None,
    )
    mapping_b = WealthfolioAccountMapping(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        target_id=str(target_b.id),
        account_id=str(account_b.id),
        wf_account_name="Portfolio B",
        wf_account_id="remote-b",
        provider_account_id="provider-b",
    )
    session.add_all([mapping_a, mapping_b])
    await session.commit()

    issues = await DataHealthService(
        session, str(tenant_a.id), permissions={"destinations:write"}
    )._destination_parity_issues()

    assert {issue.category for issue in issues} == {"destination_drift"}
    assert all("should not leak" not in issue.details for issue in issues)
    assert any(issue.blocking for issue in issues)
    assert any(issue.account_ids == [str(account_a.id)] for issue in issues)


async def test_data_health_semantic_transaction_duplicates_are_tenant_scoped(
    session,
) -> None:
    """Missing provider IDs are grouped only inside the owning tenant."""
    tenant_a = Tenant(slug="phase4-semantic-a", name="Semantic A")
    tenant_b = Tenant(slug="phase4-semantic-b", name="Semantic B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    account_a = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="trading212",
        external_account_id="account-a",
        name="Portfolio A",
        account_type="brokerage",
        currency_code="EUR",
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="trading212",
        external_account_id="account-b",
        name="Portfolio B",
        account_type="brokerage",
        currency_code="EUR",
    )
    session.add_all([account_a, account_b])
    await session.flush()

    common = {
        "provider_key": "trading212",
        "amount": 100,
        "currency_code": "EUR",
        "quantity": 2,
        "occurred_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        "transaction_type": "purchase",
        "status": "booked",
    }
    session.add_all(
        [
            Transaction(
                id=uuid4(),
                tenant_id=str(tenant_a.id),
                account_id=str(account_a.id),
                external_transaction_id="",
                **common,
            ),
            Transaction(
                id=uuid4(),
                tenant_id=str(tenant_a.id),
                account_id=str(account_a.id),
                external_transaction_id=" ",
                **common,
            ),
            Transaction(
                id=uuid4(),
                tenant_id=str(tenant_b.id),
                account_id=str(account_b.id),
                external_transaction_id="",
                **common,
            ),
        ]
    )
    await session.commit()

    issues = await DataHealthService(
        session, str(tenant_a.id), permissions={"transactions:read"}
    )._transaction_semantic_duplicate_issues()

    assert len(issues) == 1
    assert issues[0].impact_count == 2
    assert issues[0].account_ids == [str(account_a.id)]
    assert issues[0].evidence["semantic_key_hash"]


async def test_data_health_account_selection_orphan_and_tombstone_checks_are_tenant_scoped(
    session,
) -> None:
    """PostgreSQL coverage for selection, provenance and delivery-scope checks."""
    tenant_a = Tenant(slug="phase4-integrity-a", name="Integrity A")
    tenant_b = Tenant(slug="phase4-integrity-b", name="Integrity B")
    session.add_all([tenant_a, tenant_b])
    await session.flush()

    credential_a = Credential(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        encrypted_payload=b"ciphertext-a",
        nonce=b"nonce-a",
        selected_accounts=["present-a", "missing-a"],
    )
    account_a = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        connection_id=str(credential_a.id),
        external_account_id="present-a",
        name="Present A",
        account_type="checking",
        currency_code="EUR",
    )
    orphan_a = Account(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        connection_id=str(uuid4()),
        external_account_id="orphan-a",
        name="Orphan A",
        account_type="checking",
        currency_code="EUR",
    )
    transaction_a = Transaction(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        provider_key="bunq",
        connection_id=str(credential_a.id),
        external_transaction_id="transaction-a-tombstoned",
        account_id=str(account_a.id),
        amount=Decimal("1.00"),
        currency_code="EUR",
        occurred_at=datetime(2026, 8, 19, tzinfo=UTC),
        transaction_type="deposit",
        status="booked",
        tombstoned_at=datetime(2026, 8, 22, tzinfo=UTC),
    )
    stale_cursor = SyncCursor(
        id=uuid4(),
        tenant_id=str(tenant_a.id),
        connector="bunq",
        connection_id=str(credential_a.id),
        resource="present-a",
        cursor=datetime(2026, 8, 20, tzinfo=UTC),
    )
    latest_run = SyncRun(
        id=uuid4(),
        connector="bunq",
        connection_id=str(credential_a.id),
        status="completed",
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        completed_at=datetime(2026, 8, 25, 0, 1, tzinfo=UTC),
        cursor=datetime(2026, 8, 25, tzinfo=UTC),
        report={
            "accounts": 1,
            "account_external_ids": ["present-a"],
            "transactions": 1,
            "holdings": 0,
        },
        warnings=[],
    )

    credential_b = Credential(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="bunq",
        encrypted_payload=b"ciphertext-b",
        nonce=b"nonce-b",
        selected_accounts=["missing-b"],
    )
    account_b = Account(
        id=uuid4(),
        tenant_id=str(tenant_b.id),
        provider_key="bunq",
        connection_id=str(credential_b.id),
        external_account_id="present-b",
        name="Present B",
        account_type="checking",
        currency_code="EUR",
    )
    session.add_all(
        [
            credential_a,
            account_a,
            orphan_a,
            transaction_a,
            stale_cursor,
            latest_run,
            credential_b,
            account_b,
        ]
    )
    await session.flush()
    session.add(
        WealthfolioDelivery(
            id=uuid4(),
            tenant_id=str(tenant_a.id),
            target_id="target-a",
            account_id=str(account_a.id),
            last_exported_transaction_id=str(transaction_a.id),
            last_exported_at=datetime(2026, 8, 21, tzinfo=UTC),
        )
    )
    await session.commit()

    service = DataHealthService(session, str(tenant_a.id), permissions={"*:*"})
    selected = await service._selected_account_issues()
    orphaned = await service._orphaned_account_issues()
    tombstones = await service._tombstoned_export_issues()
    sync_integrity = await service._sync_integrity_issues()

    assert len(selected) == 1
    assert selected[0].evidence["missing_external_account_ids"] == ["missing-a"]
    assert len(orphaned) == 1
    assert orphaned[0].evidence["reason"] == "missing_connection"
    assert orphaned[0].account_ids == [str(orphan_a.id)]
    assert len(tombstones) == 1
    assert tombstones[0].evidence["target_id"] == "target-a"
    assert tombstones[0].affected_transaction_ids == [str(transaction_a.id)]
    assert any(
        issue.id.startswith("sync-stale-cursor:") for issue in sync_integrity
    )
    assert any(
        issue.id.startswith("sync-selected-account-gap:")
        for issue in sync_integrity
    )
    assert str(account_b.id) not in {
        item for issue in orphaned for item in issue.account_ids
    }
