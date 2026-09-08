"""Focused tests for adapter and enrichment edge paths.

These tests deliberately exercise the provider-facing code with in-memory
fakes.  They keep coverage useful without requiring live broker or SEC calls.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from unittest.mock import patch


def test_enrichment_helpers_normalise_options_and_tickers() -> None:
    from finance_sync.api.v1.enrichment import (
        _credential_values,
        _options,
        _ticker_variants,
    )
    from finance_sync.models.credential import Credential

    assert _credential_values({1: "secret", "key": 3}) == {
        "1": "secret",
        "key": "3",
    }
    assert _credential_values("not-a-dict") == {}
    credential = Credential(description='{"region":"eu","_label":"x"}')
    assert _options(credential) == {"region": "eu"}
    credential.description = "invalid"
    assert _options(credential) == {}
    assert _ticker_variants("lse:ABC") == {"LSE:ABC", "ABC"}
    assert _ticker_variants(None) == set()


@pytest.mark.asyncio
async def test_enrichment_status_builds_summary_from_queries() -> None:
    from finance_sync.api.v1.enrichment import get_enrichment_status

    class Result:
        def __init__(self, scalar=None, rows=()):
            self._scalar = scalar
            self._rows = rows

        def scalar(self):
            return self._scalar

        def __iter__(self):
            return iter(self._rows)

    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(7),
                Result(rows=[("pending", 2), ("failed", 1)]),
                Result(4),
                Result(3),
                Result(datetime(2026, 1, 1, tzinfo=UTC)),
                Result(rows=[("trading212",), ("openbb",)]),
            ]
        )
    )
    result = await get_enrichment_status(MagicMock(), session)
    assert result.total_securities == 7
    assert result.enriched_securities == 4
    assert result.pending_securities == 2
    assert result.failed_securities == 1
    assert result.stale_securities == 3
    assert result.data_sources == ["trading212", "openbb"]


def test_sec_helpers_cover_normalisation_and_retry_parsing() -> None:
    from finance_sync.intel.adapters.sec import (
        _filing_url,
        _has_event_item,
        _parse_filing_date,
        _parse_retry_after,
        _recent_submissions,
        _resolve_cik,
        _str_list,
        _str_list_list,
    )

    assert _recent_submissions({"recent": {"form": ["8-K"]}})["form"] == ["8-K"]
    assert _recent_submissions({}) == {}
    assert _str_list([1, None]) == ["1", "None"]
    assert _str_list("bad") == []
    assert _str_list_list([["1", 2], "3", None]) == [["1", "2"], ["3"]]
    assert _str_list_list("bad") == []
    assert _filing_url("320193", "0001-23-000004", "doc.htm").endswith(
        "/000123000004/doc.htm"
    )
    assert _filing_url("320193", "0001-23-000004", "").endswith(
        "/000123000004/"
    )
    assert _resolve_cik({"cik": "CIK-320193"}) == "0000320193"
    assert _resolve_cik({"ticker": "AAPL"}) is None
    assert _parse_filing_date("2026-01-02") == datetime(2026, 1, 2, tzinfo=UTC)
    assert _parse_filing_date("bad") is None
    assert _has_event_item(["2.02"]) is False
    assert _has_event_item(["5.02"]) is True
    assert _parse_retry_after("2.5") == 2.5
    assert _parse_retry_after("invalid") is None
    assert _parse_retry_after(None) is None


@pytest.mark.asyncio
async def test_sec_provider_fetches_events_and_earnings() -> None:
    from finance_sync.intel.adapters.sec import SecEdgarProvider
    from finance_sync.intel.enums import IntelCapability

    provider = SecEdgarProvider()
    provider._get_submissions = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "recent": {
                "form": ["10-K", "8-K", "8-K", "8-K"],
                "filingDate": ["2026-01-01"] * 4,
                "accessionNumber": ["", "0001-23-000004", "0001-23-000005", ""],
                "primaryDocument": ["", "event.htm", "earnings.htm", ""],
                "items": [[], ["5.02"], ["2.02"], ["8.01"]],
            }
        }
    )
    events = await provider.fetch(
        IntelCapability.CORPORATE_EVENTS, identifiers={"cik": "320193"}
    )
    earnings = await provider.fetch(
        IntelCapability.EARNINGS, identifiers={"cik": "320193"}
    )
    assert len(events) == 1
    assert events[0].kind.value == "corporate_event"
    assert len(earnings) == 1
    assert earnings[0].kind.value == "earnings_report"
    with pytest.raises(Exception):
        await provider.fetch(IntelCapability.EARNINGS)
    await provider.close()


@pytest.mark.asyncio
async def test_sec_provider_availability_and_http_client() -> None:
    from finance_sync.intel.adapters.sec import SecEdgarProvider
    from finance_sync.intel.enums import IntelAvailability, IntelCapability

    provider = SecEdgarProvider(request_timeout=4)
    response = MagicMock(status_code=200)
    provider.http_client.get = AsyncMock(return_value=response)
    assert (
        await provider.available(IntelCapability.EARNINGS)
        == IntelAvailability.AVAILABLE
    )
    response.status_code = 403
    assert (
        await provider.available(IntelCapability.EARNINGS)
        == IntelAvailability.UNAVAILABLE
    )
    response.status_code = 500
    assert (
        await provider.available(IntelCapability.EARNINGS)
        == IntelAvailability.DEGRADED
    )
    provider.http_client.get = AsyncMock(
        side_effect=httpx.TimeoutException("x")
    )
    assert (
        await provider.available(IntelCapability.EARNINGS)
        == IntelAvailability.UNAVAILABLE
    )
    assert await provider.capabilities()
    await provider.close()


@pytest.mark.asyncio
async def test_actual_budget_client_account_and_transaction_operations(
    monkeypatch,
) -> None:
    import actual.queries as queries

    from finance_sync.exporter.actual_budget.client import ActualBudgetClient
    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig

    account = SimpleNamespace(id="a1", name="Checking", offbudget=False)
    category = SimpleNamespace(id="c1", name="Food")
    transaction = SimpleNamespace(id="t1")
    queries.get_accounts = lambda session: [account]
    queries.get_account = lambda session, name: (
        account if name == "Checking" else None
    )
    queries.create_account = lambda *args, **kwargs: account
    queries.get_or_create_category = lambda *args, **kwargs: category
    queries.create_transaction = lambda *args, **kwargs: transaction
    queries.get_transactions = lambda *args, **kwargs: [transaction]
    queries.create_transfer = lambda *args, **kwargs: (transaction, transaction)
    queries.create_budget = lambda *args, **kwargs: None

    client = ActualBudgetClient(ActualBudgetConfig())
    client._actual = SimpleNamespace(session=object(), commit=lambda: None)
    assert await client.get_accounts() == [
        {"id": "a1", "name": "Checking", "offbudget": False}
    ]
    assert await client.get_account_by_name("missing") is None
    assert (await client.get_or_create_account("Checking"))["id"] == "a1"
    assert (await client.create_account("New"))["id"] == "a1"
    assert await client.ensure_category("Food") == {"id": "c1", "name": "Food"}
    assert await client.transfer_exists("ref") is True
    assert await client.create_transfer(
        date=date.today(),
        source_account="Checking",
        destination_account="Savings",
        amount=100,
        notes="move",
    ) == ("t1", "t1")
    await client.set_budget(month=date.today(), category="Food", amount=100)
    assert (
        await client.create_transaction(date=date.today(), account="Checking")
        == "t1"
    )
    assert (
        await client.create_transactions_batch(
            [{"date": date.today(), "account": "Checking"}, {"bad": True}]
        )
        == 1
    )
    assert client.is_connected is True
    await client._shutdown()


def test_actual_budget_config_from_settings() -> None:
    from pydantic import SecretStr

    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig

    settings = SimpleNamespace(
        actual_budget_server_url="http://actual",
        actual_budget_password=SecretStr("pw"),
        actual_budget_sync_id="sync",
        actual_budget_budget_name=None,
        actual_budget_encryption_password=None,
        actual_budget_verify_ssl=False,
        actual_budget_request_timeout=12.0,
        actual_budget_batch_size=20,
        actual_budget_default_off_budget=True,
        actual_budget_account_name_overrides={"a": "Checking"},
        actual_budget_transfer_account_name_overrides=None,
    )
    result = ActualBudgetConfig.from_settings(settings)
    assert result.password == "pw"
    assert result.sync_id == "sync"
    assert result.transfer_account_name_overrides == {}


def test_destination_helpers_cover_validation_and_parity() -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1.destinations import (
        TargetCreate,
        _activity_parity_counts,
        _actual_account_mapping_preview,
        _missing_wealthfolio_account_mappings,
        _parity_summary,
        _safe_url,
        _validate_body,
        _wealthfolio_provider_account_id,
    )

    assert _wealthfolio_provider_account_id({"provider_account_id": "a"}) == "a"
    assert _wealthfolio_provider_account_id({}) == ""
    mappings = [
        SimpleNamespace(provider_account_id="a"),
        SimpleNamespace(provider_account_id=None),
    ]
    assert len(_missing_wealthfolio_account_mappings(mappings, {"a"})) == 1
    assert _activity_parity_counts(
        [("x", None), ("y", datetime.now(UTC)), (None, None)],
        [{"sourceRecordId": "x"}, {"externalTransactionId": "z"}],
    ) == (1, 0, 1)
    assert _parity_summary(
        "healthy",
        {"remote_accounts": 2, "ignored": 9, "stale_remote_activities": -1},
    ) == {
        "status": "healthy",
        "counts": {"remote_accounts": 2, "stale_remote_activities": 0},
    }
    assert _safe_url("http://localhost:5006/") == "http://localhost:5006"
    assert _safe_url("https://example.com") == "https://example.com"
    with pytest.raises(HTTPException):
        _safe_url("http://example.com")
    with pytest.raises(HTTPException):
        _safe_url("https://user:pass@example.com")
    assert _actual_account_mapping_preview(
        [("a1", "Checking"), ("a2", "Savings")],
        [{"id": "remote-1", "name": "Checking", "offbudget": True}],
        default_off_budget=False,
    ) == [
        {
            "id": "a1",
            "name": "Checking",
            "action": "use_existing",
            "actual_budget_account_id": "remote-1",
            "actual_budget_account_name": "Checking",
            "off_budget": True,
        },
        {
            "id": "a2",
            "name": "Savings",
            "action": "create_on_first_sync",
            "actual_budget_account_id": None,
            "actual_budget_account_name": None,
            "off_budget": False,
        },
    ]
    valid = TargetCreate(
        target_type="jupyter",
        display_name="Notebook",
        datasets=["transactions"],
    )
    _validate_body(valid, "jupyter")
    with pytest.raises(HTTPException):
        _validate_body(
            TargetCreate(
                target_type="firefly",
                display_name="x",
                configuration={"token": "bad"},
            ),
            "firefly",
        )


def test_connector_config_helpers_cover_schemas_and_redaction(
    monkeypatch,
) -> None:
    from finance_sync.api.v1 import connectors_config as module
    from finance_sync.models.credential import Credential

    assert module._account_enumeration_error_is_fatal("bunq") is True
    assert module._account_enumeration_error_is_fatal("trading212") is False
    for connector in (
        "bunq",
        "trading212",
        "degiro_pension",
        "saxo_investor",
        "unknown",
    ):
        credentials, options = module._get_connector_credential_schema(
            connector
        )
        assert isinstance(credentials, list)
        assert isinstance(options, list)
    credentials, _ = module._staging_connector_schema("bunq")
    assert credentials[0]["required"] is False
    cred = Credential(
        id="c1",
        tenant_id="t1",
        provider_key="degiro_pension",
        description='{"_label":"Pension","watchfolder":"/tmp"}',
        encrypted_payload=b"",
        nonce=b"",
        status="active",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    response = module._credential_response(cred)
    assert response.description == "Pension"
    assert response.is_configured is True
    assert module._credential_secrets(cred, SimpleNamespace()) == []


def test_wealthfolio_export_helpers_cover_cash_holdings_and_payloads() -> None:
    from decimal import Decimal

    from finance_sync.exporter.wealthfolio.exporter import (
        _cash_reconciliation_activity,
        _cash_reconciliation_external_id,
        _decimal_or_none,
        _holdings_quantity_corrections,
        _holdings_snapshot_payload,
        _is_cash_wealthfolio_holding,
        _looks_like_isin,
        _manual_holdings_payload,
        _normalise_wealthfolio_symbol,
        _supports_multi_currency_cash,
        _wealthfolio_cash_value,
        _wf_row_to_api_activity,
    )

    assert _supports_multi_currency_cash(
        SimpleNamespace(provider_metadata={"supports_multi_currency_cash": 1})
    )
    assert not _supports_multi_currency_cash(
        SimpleNamespace(provider_metadata=None)
    )
    assert _decimal_or_none("1.25") == Decimal("1.25")
    assert _decimal_or_none("bad") is None
    rows = [
        {"holdingType": "cash", "marketValue": {"base": "10.5"}},
        {"instrument": {"id": "cash:EUR"}, "marketValue": {"base": "2"}},
        {"symbol": "ABC", "marketValue": {"base": "100"}},
    ]
    assert _wealthfolio_cash_value(rows) == Decimal("12.5")
    assert _is_cash_wealthfolio_holding(rows[0])
    assert _is_cash_wealthfolio_holding(rows[1])
    assert not _is_cash_wealthfolio_holding(rows[2])
    assert _cash_reconciliation_external_id("t", "a").endswith(":t:a")
    deposit = _cash_reconciliation_activity(
        account_id="remote",
        account_currency="EUR",
        delta=Decimal("12"),
        tenant_id="t",
        finance_sync_account_id="a",
    )
    assert deposit["activityType"] == "DEPOSIT"
    assert (
        _cash_reconciliation_activity(
            account_id="remote",
            account_currency="EUR",
            delta=Decimal("-2"),
            tenant_id="t",
            finance_sync_account_id="a",
        )["activityType"]
        == "WITHDRAWAL"
    )
    assert _looks_like_isin("US0378331005")
    assert not _looks_like_isin("AAPL")
    assert _normalise_wealthfolio_symbol("xetra:VWCE.DE") == "XETRA:VWCE"
    assert _normalise_wealthfolio_symbol("US0378331005") == "US0378331005"

    row = {
        "activityType": "BUY",
        "date": "2026-01-01",
        "symbol": "US0378331005",
        "quantity": "2",
        "unitPrice": "3.5",
        "amount": "0",
        "fee": "1",
        "currency": "USD",
        "needsReview": "yes",
        "tax": "0.2",
        "metadata": {"source": "test"},
    }
    activity = _wf_row_to_api_activity(row, account_id="a")
    assert activity["isin"] == "US0378331005"
    assert activity["quantity"] == 2.0
    assert activity["accountId"] == "a"
    assert activity["needsReview"] is True
    snapshot = _holdings_snapshot_payload(
        [
            {
                "symbol": "ABC",
                "isin": "",
                "quantity": "2",
                "avgCost": "3",
                "currency": "EUR",
                "snapshotPrice": "4",
            },
            {
                "symbol": "ZERO",
                "quantity": "0",
                "avgCost": "1",
                "currency": "EUR",
            },
        ],
        cash_balance=Decimal("5"),
        cash_currency="EUR",
    )
    assert len(snapshot["positions"]) == 1
    assert snapshot["cashBalances"] == {"EUR": "5"}
    manual = _manual_holdings_payload(snapshot)
    assert manual[0]["quoteMode"] == "MANUAL"
    corrections = _holdings_quantity_corrections(
        source_rows=[{"symbol": "ABC", "quantity": "3", "currency": "EUR"}],
        remote_rows=[
            {"instrument": {"symbol": "ABC", "id": "asset-1"}, "quantity": "1"},
            {"instrument": {"symbol": "OLD", "id": "asset-2"}, "quantity": "2"},
        ],
        account_id="remote",
        account_currency="EUR",
        tenant_id="t",
        finance_sync_account_id="a",
    )
    assert {item["activityType"] for item in corrections} == {"BUY", "SELL"}


@pytest.mark.asyncio
async def test_enrichment_endpoints_handle_missing_connections() -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1.enrichment import (
        refresh_quotes,
        refresh_trading212_identities,
        trading212_latest_quote,
    )

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

        def __iter__(self):
            return iter(())

    session = SimpleNamespace(execute=AsyncMock(return_value=EmptyResult()))
    auth = SimpleNamespace(tenant_id="tenant-1")
    with pytest.raises(HTTPException) as latest:
        await trading212_latest_quote("AAPL", auth, session, SimpleNamespace())
    assert latest.value.status_code == 404
    with pytest.raises(HTTPException) as refresh:
        await refresh_trading212_identities(auth, session, SimpleNamespace())
    assert refresh.value.status_code == 404
    assert (await refresh_quotes(auth, session, SimpleNamespace()))[
        "status"
    ] == "unavailable"


def test_firefly_helpers_and_result_objects() -> None:
    from decimal import Decimal

    from finance_sync.exporter.firefly.config import FireflyConfig
    from finance_sync.exporter.firefly.exporter import (
        FireflyExportResult,
        FireflyExporter,
        _has_firefly_amount,
    )

    assert _has_firefly_amount(SimpleNamespace(amount=Decimal("1")))
    assert not _has_firefly_amount(SimpleNamespace(amount=0))
    assert not _has_firefly_amount(SimpleNamespace(amount="bad"))
    result = FireflyExportResult(status="completed", accounts_mapped=2)
    assert result.status == "completed"
    exporter = FireflyExporter(
        MagicMock(),
        FireflyConfig(
            budget_name_map={"food": "Food Budget"},
            bill_name_map={"food": "Food Bill"},
        ),
        "tenant-1",
    )
    txn = SimpleNamespace(cashflow_suggestion={"value": "food"})
    assert exporter._firefly_category_key(txn) == "food"
    assert exporter._firefly_budget_name(txn) == "Food Budget"
    assert exporter._firefly_bill_name(txn) == "Food Bill"
    txn.cashflow_suggestion = SimpleNamespace(value="travel")
    assert exporter._firefly_category_key(txn) == "travel"


def test_data_quality_and_deletion_value_helpers() -> None:
    from finance_sync.services.connector_data_deletion import (
        ConnectorDeletionPreview,
    )
    from finance_sync.services.data_quality_repair import (
        DataQualityRepairService,
    )

    assert DataQualityRepairService._instrument_rows(
        [{"ticker": "ABC"}, "ignored", 3]
    ) == [{"ticker": "ABC"}]
    assert DataQualityRepairService._instrument_rows("invalid") == []
    credential = SimpleNamespace(description='{"region":"eu","_label":"x"}')
    assert DataQualityRepairService._options(credential) == {"region": "eu"}
    credential.description = "invalid"
    assert DataQualityRepairService._options(credential) == {}
    preview = ConnectorDeletionPreview(
        provider_key="bunq",
        connection_id="c1",
        accounts=1,
        transactions=2,
        card_transactions=0,
        holdings=1,
        balances=1,
        other_records=3,
        legacy_records_warning="legacy",
    )
    values = preview.as_dict()
    assert values["provider_key"] == "bunq"
    assert values["other_records"] == 3


def test_wealthfolio_reconciliation_reports_quantity_and_value_drift() -> None:
    from decimal import Decimal

    from finance_sync.exporter.wealthfolio.exporter import (
        WealthfolioExportResult,
        _reconcile_holdings,
    )

    account = SimpleNamespace(
        id="a1",
        name="Checking",
        available_balance=Decimal("10"),
        current_balance=None,
    )
    findings = _reconcile_holdings(
        account=account,
        source_rows=[
            {
                "symbol": "ABC.DE",
                "isin": "US0378331005",
                "quantity": "2",
                "snapshotPrice": "5",
            }
        ],
        remote_rows=[
            {
                "instrument": {"symbol": "ABC", "isin": "US0378331005"},
                "quantity": "1",
                "marketValue": {"base": "1"},
            },
            {
                "instrument": {"symbol": "EXTRA"},
                "quantity": "1",
                "marketValue": {"base": "1"},
            },
            {
                "holdingType": "cash",
                "symbol": "EUR",
                "marketValue": {"base": "10"},
            },
        ],
        absolute_tolerance=Decimal("0.01"),
        percentage_tolerance=Decimal("0.01"),
    )
    assert len(findings) == 3
    assert any("Positie" in item["error"] for item in findings)
    assert any("bronsnapshot" in item["error"] for item in findings)
    assert any("Portefeuillewaarde" in item["error"] for item in findings)
    result = WealthfolioExportResult(status="failed", csv_files=["a.csv"])
    assert "failed" in repr(result)


@pytest.mark.asyncio
async def test_firefly_exporter_handles_missing_token_and_empty_success(
    monkeypatch,
) -> None:
    from finance_sync.exporter.firefly.config import FireflyConfig
    from finance_sync.exporter.firefly.exporter import FireflyExporter

    class Session:
        def __init__(self):
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def merge(self, value):
            return value

    factory = MagicMock(side_effect=lambda: Session())
    missing = FireflyExporter(factory, FireflyConfig(), "tenant")
    failed = await missing.run_export()
    assert failed.status == "failed"
    assert "ACCESS_TOKEN" in (failed.error_message or "")

    config = FireflyConfig(access_token="token")
    exporter = FireflyExporter(factory, config, "tenant")
    exporter._load_accounts = AsyncMock(return_value=[])  # type: ignore[method-assign]
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "finance_sync.exporter.firefly.exporter.FireflyClient",
        lambda *_args, **_kwargs: fake_client,
    )
    completed = await exporter.run_export()
    assert completed.status == "completed"
    assert completed.accounts_mapped == 0
    fake_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_firefly_client_resolve_or_create_and_split_transactions() -> (
    None
):
    from finance_sync.exporter.firefly.client import (
        FireflyClient,
        FireflyClientConfig,
    )

    client = FireflyClient(
        FireflyClientConfig("http://firefly", "token", retry_base_delay=0)
    )

    async def request(method, path, **kwargs):
        if path == "/api/v1/about":
            return {"version": "1"}
        if method == "GET" and path == "/api/v1/accounts":
            return {"data": [{"id": "a1", "attributes": {"name": "Checking"}}]}
        if method == "GET" and path == "/api/v1/categories":
            return {"data": [{"id": "c1", "attributes": {"name": "Food"}}]}
        if method == "GET" and path == "/api/v1/tags":
            return {"data": [{"id": "t1", "attributes": {"tag": "Tag"}}]}
        if method == "GET" and path == "/api/v1/budgets":
            return {"data": [{"id": "b1", "attributes": {"name": "Budget"}}]}
        if method == "GET" and path == "/api/v1/bills":
            return {"data": [{"id": "bill1", "attributes": {"name": "Bill"}}]}
        return {"data": {"id": "tx1"}}

    client._request = AsyncMock(side_effect=request)  # type: ignore[method-assign]
    assert await client.about() == {"version": "1"}
    assert await client.ensure_asset_account(
        name="Checking", currency_code="EUR"
    ) == {"id": "a1", "name": "Checking"}
    assert await client.ensure_category("Food") == {"id": "c1", "name": "Food"}
    assert await client.ensure_tag("tag") == {"id": "t1", "tag": "Tag"}
    assert await client.ensure_budget("Budget", currency_code="EUR") == {
        "id": "b1",
        "name": "Budget",
    }
    assert await client.ensure_bill("Bill") == {"id": "bill1", "name": "Bill"}
    remote = await client.store_transaction(
        {
            "type": "withdrawal",
            "date": "2026-01-01",
            "external_id": "x",
            "canonical_splits": [{"amount": "2", "category": "Food"}],
        }
    )
    assert remote == {"id": "tx1"}
    await client.close()


@pytest.mark.asyncio
async def test_destination_metadata_endpoints_are_complete() -> None:
    from finance_sync.api.v1.destinations import (
        list_destination_capabilities,
        list_types,
    )

    types = await list_types()
    assert {item["key"] for item in types} >= {
        "jupyter",
        "wealthfolio",
        "firefly",
    }
    capabilities = await list_destination_capabilities()
    assert "wealthfolio" in capabilities
    assert capabilities["firefly"]


@pytest.mark.asyncio
async def test_destination_account_validation_and_scope() -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1.destinations import (
        _jupyter_account_scope,
        _validate_accounts,
    )

    db = SimpleNamespace(
        scalar=AsyncMock(return_value=2),
        scalars=AsyncMock(return_value=["a", "b"]),
    )
    await _validate_accounts(db, "tenant", ["a", "b"])
    assert await _jupyter_account_scope(db, "tenant", ["chosen"]) == ["chosen"]
    assert await _jupyter_account_scope(db, "tenant", []) == ["a", "b"]
    with pytest.raises(HTTPException, match="unique"):
        await _validate_accounts(db, "tenant", ["a", "a"])
    db.scalar.return_value = 1
    with pytest.raises(HTTPException, match="does not belong"):
        await _validate_accounts(db, "tenant", ["a", "b"])


@pytest.mark.asyncio
async def test_trading212_quote_and_identity_refresh_happy_paths(
    monkeypatch,
) -> None:
    from finance_sync.api.v1 import enrichment as module
    from finance_sync.models.credential import Credential

    credential = Credential(
        id="cred-1",
        tenant_id="tenant-1",
        provider_key="trading212",
        status="active",
        encrypted_payload=b"secret",
        nonce=b"n",
        description='{"demo":true}',
    )
    record = SimpleNamespace(
        external_security_id="ABC_EQ",
        raw_ticker="ABC_EQ",
        raw_isin=None,
        raw_name=None,
        raw_currency_code=None,
        raw_metadata=None,
        resolved_security_id=None,
        resolution_method=None,
    )
    candidate = SimpleNamespace(id="security-1")

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

        def __iter__(self):
            return iter(self.values)

    connector = SimpleNamespace(
        authenticate=AsyncMock(),
        fetch_portfolio=AsyncMock(
            return_value=[
                {
                    "ticker": "ABC_EQ",
                    "currentPrice": "12.5",
                    "currencyCode": "USD",
                }
            ]
        ),
        fetch_instruments=AsyncMock(
            return_value=[
                {
                    "ticker": "ABC_EQ",
                    "isin": "US0000000001",
                    "name": "ABC Inc",
                    "currencyCode": "USD",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        module, "decrypt_credential", lambda *args: '{"api_key":"x"}'
    )
    monkeypatch.setattr(
        module.ConnectorRegistry,
        "get_connector",
        lambda self, config: connector,
    )
    auth = SimpleNamespace(tenant_id="tenant-1")
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result([credential]), Result([record])]),
        scalar=AsyncMock(return_value=candidate),
        commit=AsyncMock(),
    )
    quote = await module.trading212_latest_quote(
        "ABC_EQ", auth, session, SimpleNamespace()
    )
    assert quote["price"] == "12.5"
    assert quote["currency"] == "USD"
    session.execute.side_effect = [Result([credential]), Result([record])]
    refreshed = await module.refresh_trading212_identities(
        auth, session, SimpleNamespace()
    )
    assert refreshed["unresolved_updated"] == 1
    assert refreshed["resolved_by_isin"] == 1
    assert record.resolution_method == "auto_isin"


@pytest.mark.asyncio
async def test_data_quality_repair_repairs_provider_identity(
    monkeypatch,
) -> None:
    from finance_sync.services import data_quality_repair as module
    from finance_sync.services.data_quality_repair import (
        DataQualityRepairService,
    )

    credential = SimpleNamespace(
        encrypted_payload=b"secret", nonce=b"nonce", description="{}"
    )
    record = SimpleNamespace(
        external_security_id="ABC",
        raw_ticker=None,
        raw_isin=None,
        raw_name=None,
        raw_currency_code=None,
        raw_metadata=None,
        resolved_security_id=None,
        resolution_method=None,
    )
    candidate = SimpleNamespace(id="sec-1")

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def __iter__(self):
            return iter(self.rows)

    connector = SimpleNamespace(
        authenticate=AsyncMock(),
        fetch_instruments=AsyncMock(
            return_value=[
                {
                    "ticker": "ABC",
                    "isin": "US0000000001",
                    "name": "ABC",
                    "currencyCode": "USD",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "finance_sync.services.auth.decrypt_credential",
        lambda *args: '{"api_key":"x"}',
    )
    monkeypatch.setattr(
        module.ConnectorRegistry,
        "get_connector",
        lambda self, config: connector,
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result([credential]), Result([record])]),
        scalar=AsyncMock(return_value=candidate),
    )
    service = DataQualityRepairService(session, SimpleNamespace())
    identity = await service._repair_trading212_identities("tenant-1")
    assert identity == {
        "credentials": 1,
        "instruments_fetched": 1,
        "updated": 1,
        "resolved_by_isin": 1,
    }
    assert record.resolution_method == "auto_isin"
    service._repair_trading212_identities = AsyncMock(
        return_value={"updated": 1}
    )  # type: ignore[method-assign]
    service._refresh_quotes = AsyncMock(return_value={"updated": 2})  # type: ignore[method-assign]
    service._remaining = AsyncMock(return_value={"unresolved_securities": 0})  # type: ignore[method-assign]
    report = await service.run("tenant-1")
    assert report["safe_repairs_only"] is True


@pytest.mark.asyncio
async def test_connector_deletion_preview_is_tenant_scoped() -> None:
    from finance_sync.services.connector_data_deletion import (
        ConnectorDataDeletionService,
    )

    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[]),
        scalar=AsyncMock(return_value=0),
    )
    credential = SimpleNamespace(id="connection-1", provider_key="bunq")
    preview = await ConnectorDataDeletionService(session, "tenant-1").preview(
        credential
    )
    assert preview.provider_key == "bunq"
    assert preview.connection_id == "connection-1"
    assert preview.accounts == 0
    assert preview.transactions == 0
    assert preview.other_records == 0


@pytest.mark.asyncio
async def test_connector_deletion_preview_counts_connected_accounts() -> None:
    from finance_sync.services.connector_data_deletion import (
        ConnectorDataDeletionService,
    )

    session = SimpleNamespace(
        scalars=AsyncMock(return_value=["account-1"]),
        scalar=AsyncMock(return_value=0),
    )
    preview = await ConnectorDataDeletionService(session, "tenant-1").preview(
        SimpleNamespace(id="connection-1", provider_key="trading212")
    )
    assert preview.accounts == 1
    assert preview.transactions == 0
    assert session.scalar.await_count > 10


def test_subscription_response_helpers_serialize_enums_and_decimals() -> None:
    from decimal import Decimal

    from finance_sync.api.v1.subscriptions import (
        _sub_from_detection,
        _sub_to_response,
    )
    from finance_sync.models.enums import (
        DetectionMethod,
        SubscriptionConfidence,
        SubscriptionStatus,
    )
    from finance_sync.services.subscription_detector.service import Subscription

    now = datetime(2026, 1, 1, tzinfo=UTC)
    detected = Subscription(
        merchant_name="Streaming Co",
        raw_description="Monthly charge",
        amount=Decimal("12.34"),
        currency_code="EUR",
        frequency_days=30,
        frequency_label="monthly",
        confidence=SubscriptionConfidence.HIGH,
        detection_score=0.95,
        detection_method=DetectionMethod.HYBRID,
        status=SubscriptionStatus.ACTIVE,
        transaction_ids=["tx-1"],
        account_id="account-1",
        provider_key="bunq",
        category="streaming",
        sector="media",
        first_detected_at=now,
        last_detected_at=now,
        occurrence_count=4,
    )
    result = _sub_from_detection(detected)
    assert result.confidence == SubscriptionConfidence.HIGH.value
    assert result.detection_method == DetectionMethod.HYBRID.value
    assert result.status == SubscriptionStatus.ACTIVE.value
    assert (
        _sub_to_response(
            SimpleNamespace(
                id="s1",
                merchant_name="Shop",
                raw_description="raw",
                amount=Decimal("2"),
                currency_code="EUR",
                frequency_days=7,
                frequency_label="weekly",
                confidence="high",
                detection_method="pattern",
                status="active",
                account_id=None,
                provider_key="bunq",
                sector=None,
                category=None,
                security_id=None,
                fundamentals_available=False,
                first_detected_at=now,
                last_detected_at=now,
                occurrence_count=2,
                detection_score=0.8,
                details={},
                user_notes=None,
                created_at=now,
            )
        ).merchant_name
        == "Shop"
    )


def test_spending_and_datamart_schemas_cover_validation_and_serialization() -> (
    None
):
    from decimal import Decimal

    from pydantic import ValidationError

    from finance_sync.api.v1.datamarts import (
        ConsumerCreate,
        DataMartCreate,
        GrantCreate,
        _consumer_response,
        _mart_response,
    )
    from finance_sync.api.v1.spending import _dump

    with pytest.raises(ValidationError):
        DataMartCreate(
            key="mart",
            display_name="Mart",
            dataset="transactions",
            schema_version="pfc/1.0",
            fields=["amount", "amount"],
            delivery_method="api",
        )
    mart_request = DataMartCreate(
        key="mart",
        display_name="Mart",
        dataset="transactions",
        schema_version="pfc/1.0",
        fields=["amount", "currency"],
        delivery_method="pull_api",
    )
    mart = SimpleNamespace(
        id="m1",
        key=mart_request.key,
        display_name="Mart",
        dataset="transactions",
        schema_version="pfc/1.0",
        fields=mart_request.fields,
        delivery_method="pull_api",
        delivery_config={},
        is_active=True,
    )
    assert _mart_response(mart).id == "m1"
    consumer = SimpleNamespace(
        id="c1",
        key="consumer",
        display_name="Consumer",
        api_key_id=None,
        is_active=True,
    )
    assert _consumer_response(consumer).key == "consumer"
    assert (
        ConsumerCreate(key="consumer", display_name="Consumer").api_key_id
        is None
    )
    assert (
        GrantCreate(consumer_id="c1", datamart_id="m1").household_scope
        == "explicit"
    )
    with pytest.raises(ValidationError):
        GrantCreate(
            consumer_id="c1", datamart_id="m1", allowed_fields=["x", "x"]
        )
    row = SimpleNamespace(
        id="r1", object_type="source", amount=Decimal("2"), ignored=None
    )
    dumped = _dump(row)
    assert dumped == {
        "id": "r1",
        "object_type": "source",
        "amount": Decimal("2"),
    }


def test_file_upload_detection_and_csv_mapping(tmp_path) -> None:
    from finance_sync.api.v1.file_uploads import (
        _credential_label,
        _csv_mapping,
        _detect,
        _inspect_path,
        _normalise,
    )

    assert (
        _credential_label('{"_label":"Pension"}', "degiro_pension") == "Pension"
    )
    assert (
        _credential_label("invalid", "manual_expense") == "Handmatige uitgaven"
    )
    assert _normalise("Datum & bedrag!") == "datumbedrag"
    csv_path = tmp_path / "degiro_rekeningoverzicht.csv"
    csv_path.write_text(
        "OrderId;Valutadatum;WaardeInEUR;Datum;Omschrijving;Bedrag\n1;2026-01-01;-10;2026-01-01;Shop;-10\n"
    )
    markers, evidence = _inspect_path(csv_path)
    assert {"degiro_filename", "degiro_content", "csv_content"} <= markers
    assert evidence
    assert _detect(markers)[0] == "degiro_pension"
    assert _csv_mapping(csv_path) == {
        "date": "Datum",
        "description": "Omschrijving",
        "amount": "Bedrag",
    }
    json_path = tmp_path / "expenses.json"
    json_path.write_text('{"expenses":[{"amount": 1}]}')
    json_markers, _ = _inspect_path(json_path)
    assert _detect(json_markers)[0] == "manual_expense"
    unknown, reason = _detect(set())
    assert unknown is None
    assert "niet betrouwbaar" in reason


def test_cli_parser_covers_all_export_commands_and_boolean_options() -> None:
    from decimal import Decimal

    from finance_sync.cli import _build_parser

    parser = _build_parser()
    cases = [
        (["reconcile"], "reconcile"),
        (["reconcile", "--no-detect-duplicates"], "reconcile"),
        (["compare", "bunq", "trading212"], "compare"),
        (["wealthfolio", "export"], "wealthfolio"),
        (["wealthfolio", "push", "--dry-run"], "wealthfolio"),
        (["wealthfolio", "smoke"], "wealthfolio"),
        (["actual-budget", "export"], "actual-budget"),
        (["actual-budget", "push", "--dry-run"], "actual-budget"),
        (["securo", "export"], "securo"),
        (["ghostfolio", "push", "--dry-run"], "ghostfolio"),
        (["investbrain", "push", "--dry-run"], "investbrain"),
    ]
    for argv, command in cases:
        parsed = parser.parse_args(argv)
        assert parsed.command == command
    assert (
        parser.parse_args(
            ["reconcile", "--no-detect-duplicates"]
        ).detect_duplicates
        is False
    )
    assert parser.parse_args(["compare", "a", "b"]).threshold_hours == 48


def test_cli_parser_rejects_missing_nested_commands() -> None:
    from finance_sync.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["ghostfolio"])


def test_destination_helpers_cover_parity_and_url_validation() -> None:
    from fastapi import HTTPException
    from finance_sync.api.v1.destinations import (
        _activity_parity_counts,
        _missing_wealthfolio_account_mappings,
        _parity_summary,
        _safe_url,
        _wealthfolio_provider_account_id,
    )

    assert _wealthfolio_provider_account_id({"providerAccountId": "p1"}) == "p1"
    assert (
        _wealthfolio_provider_account_id({"provider_account_id": "p2"}) == "p2"
    )
    assert _wealthfolio_provider_account_id({}) == ""
    mappings = [
        SimpleNamespace(provider_account_id=None),
        SimpleNamespace(provider_account_id="missing"),
    ]
    assert len(_missing_wealthfolio_account_mappings(mappings, set())) == 2
    assert _activity_parity_counts(
        [("a", None), ("b", object()), (None, None)],
        [{"sourceRecordId": "a"}, {"externalTransactionId": "stale"}],
    ) == (1, 0, 1)
    assert _parity_summary("ok", {"remote_accounts": -2, "ignored": 9}) == {
        "status": "ok",
        "counts": {"remote_accounts": 0},
    }
    assert (
        _safe_url("https://example.test/path/") == "https://example.test/path"
    )
    assert _safe_url("http://localhost:8080") == "http://localhost:8080"
    for value in [
        "relative",
        "ftp://example.test",
        "http://example.test",
        "https://user:pass@example.test",
    ]:
        with pytest.raises(HTTPException):
            _safe_url(value)


def test_wealthfolio_helpers_cover_cash_and_quantity_corrections() -> None:
    from decimal import Decimal

    from finance_sync.exporter.wealthfolio.exporter import (
        _cash_reconciliation_activity,
        _cash_reconciliation_external_id,
        _decimal_or_none,
        _holdings_quantity_corrections,
        _is_cash_wealthfolio_holding,
        _normalise_wealthfolio_symbol,
        _supports_multi_currency_cash,
        _wealthfolio_cash_value,
    )

    assert _decimal_or_none("2.5") == Decimal("2.5")
    assert _decimal_or_none("bad") is None
    assert _decimal_or_none(None) is None
    assert _supports_multi_currency_cash(
        SimpleNamespace(
            provider_metadata={"supports_multi_currency_cash": True}
        )
    )
    assert not _supports_multi_currency_cash(
        SimpleNamespace(provider_metadata=None)
    )
    cash = {
        "holdingType": "cash",
        "instrument": {"id": "cash:EUR"},
        "marketValue": {"base": "3"},
    }
    stock = {
        "holdingType": "security",
        "instrument": {"id": "asset"},
        "marketValue": {"base": "99"},
    }
    assert _is_cash_wealthfolio_holding(cash)
    assert not _is_cash_wealthfolio_holding(stock)
    assert _wealthfolio_cash_value([cash, stock]) == Decimal("3")
    assert _cash_reconciliation_external_id("t", "a").endswith(":t:a")
    activity = _cash_reconciliation_activity(
        account_id="wa",
        account_currency="EUR",
        delta=Decimal("2"),
        tenant_id="t",
        finance_sync_account_id="a",
    )
    assert activity["activityType"] == "DEPOSIT"
    assert _normalise_wealthfolio_symbol("vwce.de") == "VWCE"
    source = [{"symbol": "VWCE.DE", "quantity": "2", "currency": "EUR"}]
    remote = [
        {"symbol": "VWCE", "quantity": "1", "instrument": {"id": "asset"}}
    ]
    corrections = _holdings_quantity_corrections(
        source_rows=source,
        remote_rows=remote,
        account_id="wa",
        account_currency="EUR",
        tenant_id="t",
        finance_sync_account_id="a",
    )
    assert corrections and corrections[0]["activityType"] == "BUY"
    assert corrections[0]["quantity"] == 1.0


def test_destination_response_and_actual_mapping_helpers() -> None:
    from finance_sync.api.v1.destinations import (
        _actual_account_mapping_preview,
        _empty_destination_parity_accounts,
        _response,
    )

    assert _empty_destination_parity_accounts() == []
    row = SimpleNamespace(
        id="t",
        target_type="wealthfolio",
        display_name="Target",
        status="active",
        version=1,
        configuration={},
        selected_account_ids=[],
        datasets=[],
        encrypted_secret=None,
        jupyter_api_key_id=None,
        schedule_id=None,
        last_health_status=None,
        last_health_error=None,
        last_parity_summary={},
        last_checked_at=None,
        created_at=__import__("datetime").datetime.now(
            __import__("datetime").UTC
        ),
        updated_at=__import__("datetime").datetime.now(
            __import__("datetime").UTC
        ),
    )
    assert _response(row).id == "t"
    preview = _actual_account_mapping_preview(
        [("a", "One")],
        [{"id": "remote", "name": "One"}],
        default_off_budget=False,
    )
    assert preview


@pytest.mark.asyncio
async def test_wealthfolio_exporter_completes_cleanly_without_accounts(
    tmp_path,
) -> None:
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig

    session = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    exporter = WealthfolioExporter(
        factory, WealthfolioConfig(), tenant_id="tenant"
    )
    with (
        patch.object(
            exporter, "_load_accounts", new=AsyncMock(return_value=[])
        ),
        patch.object(exporter, "_store_preflight_manifest", new=AsyncMock()),
        patch.object(exporter, "_complete_run", new=AsyncMock()),
    ):
        result = await exporter.run_export(
            since=datetime.now(UTC), output_dir=tmp_path
        )
    assert result.status == "completed"
    session.commit.assert_awaited()


def test_cli_main_dispatches_each_command() -> None:
    from finance_sync import cli

    for argv in [
        ["reconcile"],
        ["compare", "a", "b"],
        ["wealthfolio", "export"],
        ["actual-budget", "export"],
        ["securo", "export"],
        ["ghostfolio", "push"],
        ["investbrain", "push"],
    ]:
        with patch.object(cli, "_run_async") as run:
            cli.main(argv)
            run.assert_called_once()
            run.call_args.args[0].close()


def test_import_response_models_cover_optional_fields() -> None:
    from finance_sync.api.v1.degiro_imports import _response as degiro_response
    from finance_sync.api.v1.file_uploads import FileUploadRunResponse
    from finance_sync.api.v1.saxo_imports import SaxoImportResponse

    now = datetime.now(UTC)
    run = SimpleNamespace(
        id="run",
        tenant_id="tenant",
        connection_id="conn",
        account_id="acct",
        source="degiro",
        status="completed",
        batch_hash="hash",
        file_name="file.csv",
        created_at=now,
        completed_at=now,
        imported_count=2,
        skipped_count=1,
        rejected_count=0,
        error_message=None,
        error_category=None,
        quarantined_path=None,
        preview={},
        report_types=[],
        content_hashes=[],
        file_names=[],
        period_start=None,
        period_end=None,
        rows_total=0,
        created_count=2,
        updated_count=0,
        warnings=[],
        safe_error=None,
        retained=True,
    )
    response = degiro_response(run)
    assert response.id == "run" and response.status == "completed"
    upload = FileUploadRunResponse(
        id="u",
        provider_type="manual_expense",
        file_name="x.csv",
        status="completed",
        created_at=now,
        completed_at=now,
        imported_count=1,
        skipped_count=0,
        rejected_count=0,
        error_message=None,
        file_names=["x.csv"],
        created_count=1,
        updated_count=0,
        profile_name="default",
    )
    assert upload.created_count == 1
    sax = SaxoImportResponse(
        id="s",
        tenant_id="tenant",
        connection_id="conn",
        status="failed",
        file_name="x.csv",
        created_at=now,
        completed_at=None,
        imported_count=0,
        skipped_count=0,
        rejected_count=1,
        error_message="bad",
        file_names=["x.csv"],
        accounts=0,
        transactions=0,
        holdings=0,
        unresolved_securities=0,
        message="bad",
    )
    assert sax.message == "bad"


@pytest.mark.asyncio
async def test_mcp_resources_serialize_results_and_close_sessions() -> None:
    import finance_sync.mcp.server as mcp_server

    session = SimpleNamespace(aclose=AsyncMock())
    service = SimpleNamespace(
        _session=session,
        list_accounts=AsyncMock(
            return_value=SimpleNamespace(
                model_dump=lambda: {"items": []}, items=[]
            )
        ),
        get_portfolio=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"total": 1})
        ),
        get_net_worth=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"net": 2})
        ),
    )
    with (
        patch.object(mcp_server, "_get_tenant_id", return_value="tenant"),
        patch.object(
            mcp_server, "_get_read_service", new=AsyncMock(return_value=service)
        ),
    ):
        assert '"items": []' in await mcp_server.resource_accounts(None)
        assert '"total": 1' in await mcp_server.resource_portfolio(None)
        assert '"net": 2' in await mcp_server.resource_net_worth(None)
    assert session.aclose.await_count == 3


@pytest.mark.asyncio
async def test_mcp_transaction_resource_enriches_and_sorts_accounts() -> None:
    import finance_sync.mcp.server as mcp_server

    session = SimpleNamespace(aclose=AsyncMock())
    accounts = [SimpleNamespace(id="a", name="Checking", account_type="cash")]
    account_result = SimpleNamespace(items=accounts)
    tx_result = SimpleNamespace(
        items=[
            SimpleNamespace(model_dump=lambda: {"occurred_at": "2026-01-01"})
        ]
    )
    service = SimpleNamespace(
        _session=session,
        list_accounts=AsyncMock(return_value=account_result),
        list_account_transactions=AsyncMock(return_value=tx_result),
    )
    with (
        patch.object(mcp_server, "_get_tenant_id", return_value="tenant"),
        patch.object(
            mcp_server, "_get_read_service", new=AsyncMock(return_value=service)
        ),
    ):
        payload = await mcp_server.resource_transactions(None)
    assert "Checking" in payload
    service.list_account_transactions.assert_awaited_once_with(
        "tenant", "a", limit=20
    )


def _degiro_run(**changes: object) -> SimpleNamespace:
    now = datetime.now(UTC)
    values = dict(
        id="run-1",
        connection_id="conn-1",
        source="upload",
        status="previewed",
        report_types=["portfolio"],
        content_hashes=["hash"],
        file_names=["x.csv"],
        period_start=None,
        period_end=None,
        rows_total=1,
        created_count=0,
        updated_count=0,
        skipped_count=0,
        rejected_count=0,
        warnings=[],
        error_details=[],
        preview={},
        retained=False,
        created_at=now,
        completed_at=None,
        audit_events=[],
    )
    values.update(changes)
    values["safe_error"] = (values["error_details"] or [None])[0]  # pyright: ignore[reportIndexIssue]
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_degiro_import_helpers_record_failures_and_list_runs() -> None:
    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports

    class FakeDb:
        def __init__(self) -> None:
            self.added = []
            self.flush = AsyncMock()

        def add(self, value: object) -> None:
            self.added.append(value)

    db = FakeDb()
    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    connection = SimpleNamespace(id="conn")
    await degiro_imports._record_failed_preview(
        db, "run-failed", auth, connection, "x" * 600
    )
    assert db.added[0].status == "failed"
    assert len(db.added[0].error_details[0]) == 500
    db.flush.assert_awaited_once()

    row = _degiro_run(error_details=["safe error"], status="failed")
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))
    listed_db = SimpleNamespace(execute=AsyncMock(return_value=result))
    request = SimpleNamespace()
    with (
        patch.object(
            degiro_imports,
            "get_container",
            return_value=SimpleNamespace(settings=SimpleNamespace()),
        ),
        patch.object(degiro_imports, "cleanup_expired_previews"),
    ):
        responses = await degiro_imports.list_import_runs(
            request, None, auth, listed_db
        )
    assert responses[0].error == "safe error"
    listed_db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_degiro_retry_rejects_missing_and_non_quarantined_runs() -> None:
    from fastapi import HTTPException

    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports

    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )

    class ScalarResult:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one_or_none(self) -> object:
            return self.value

    for value, code in [(None, 404), (_degiro_run(status="failed"), 409)]:
        db = SimpleNamespace(
            execute=AsyncMock(return_value=ScalarResult(value))
        )
        with pytest.raises(HTTPException) as exc:
            await degiro_imports.retry_quarantined_import("run-1", auth, db)
        assert exc.value.status_code == code


@pytest.mark.asyncio
async def test_degiro_confirm_guard_branches_are_explicitly_rejected() -> None:
    from fastapi import HTTPException

    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports

    class ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    user_auth = AuthContext(
        user=SimpleNamespace(id="user", tenant_id="tenant", role="user")
    )
    admin_auth = AuthContext(
        user=SimpleNamespace(id="admin", tenant_id="tenant", role="admin")
    )
    request = SimpleNamespace()
    for auth, body, run, status_code in [
        (
            user_auth,
            degiro_imports.ConfirmRequest(force_reimport=True),
            None,
            403,
        ),
        (admin_auth, degiro_imports.ConfirmRequest(), None, 404),
        (
            admin_auth,
            degiro_imports.ConfirmRequest(),
            _degiro_run(status="failed"),
            409,
        ),
        (
            admin_auth,
            degiro_imports.ConfirmRequest(),
            _degiro_run(
                status="previewed", preview={"already_processed": True}
            ),
            409,
        ),
        (
            admin_auth,
            degiro_imports.ConfirmRequest(),
            _degiro_run(status="previewed", report_types=["transactions"]),
            422,
        ),
    ]:
        db = SimpleNamespace(execute=AsyncMock(return_value=ScalarResult(run)))
        with pytest.raises(HTTPException) as exc:
            await degiro_imports.confirm_import(
                "run-1", body, request, auth, db
            )
        assert exc.value.status_code == status_code


@pytest.mark.asyncio
async def test_degiro_retry_watchfolder_configuration_and_missing_source_guards(
    tmp_path,
) -> None:
    from fastapi import HTTPException

    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    run = _degiro_run(status="quarantined")
    db = SimpleNamespace(execute=AsyncMock(return_value=Result(run)))
    with patch.object(
        degiro_imports,
        "_connection",
        new=AsyncMock(
            return_value=SimpleNamespace(id="conn", description="{}")
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await degiro_imports.retry_quarantined_import("run", auth, db)
    assert exc.value.status_code == 409
    watchfolder = tmp_path / "watch"
    connection = SimpleNamespace(
        id="conn", description=json.dumps({"watchfolder": str(watchfolder)})
    )
    with patch.object(
        degiro_imports, "_connection", new=AsyncMock(return_value=connection)
    ):
        with pytest.raises(HTTPException) as exc:
            await degiro_imports.retry_quarantined_import("run", auth, db)
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_degiro_preview_records_all_validation_failure_categories() -> (
    None
):
    from fastapi import HTTPException

    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports
    from finance_sync.connectors.exceptions import PermanentError
    from finance_sync.services.degiro_import import ImportValidationError

    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    connection = SimpleNamespace(id="conn", description="{}")
    db = SimpleNamespace(add=MagicMock(), flush=AsyncMock(), commit=AsyncMock())
    container = SimpleNamespace(settings=SimpleNamespace())
    for error in [
        ImportValidationError("bad upload"),
        PermanentError("provider bad"),
        RuntimeError("unexpected"),
    ]:
        with (
            patch.object(
                degiro_imports,
                "_connection",
                new=AsyncMock(return_value=connection),
            ),
            patch.object(
                degiro_imports, "get_container", return_value=container
            ),
            patch.object(degiro_imports, "cleanup_expired_previews"),
            patch.object(
                degiro_imports,
                "stage_uploads",
                new=AsyncMock(side_effect=error),
            ),
            patch.object(
                degiro_imports, "report_connector_failure", new=AsyncMock()
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await degiro_imports.preview_import(
                    SimpleNamespace(), "conn", [], auth, db
                )
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_degiro_confirm_success_updates_connection_health() -> None:
    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    run = _degiro_run(
        status="previewed",
        report_types=["portfolio", "transactions", "account_statement"],
    )
    connection = SimpleNamespace(
        id="conn",
        description="{}",
        last_success_at=None,
        last_attempt_at=None,
        last_error="bad",
        last_error_category="x",
    )
    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(run)), commit=AsyncMock()
    )
    container = SimpleNamespace(settings=SimpleNamespace())

    async def execute(run, **kwargs):
        run.status = "completed"
        run.completed_at = datetime.now(UTC)

    with (
        patch.object(
            degiro_imports,
            "_connection",
            new=AsyncMock(return_value=connection),
        ),
        patch.object(degiro_imports, "get_container", return_value=container),
        patch.object(degiro_imports, "stage_paths", return_value=[]),
        patch.object(degiro_imports, "connector_options", return_value={}),
        patch.object(degiro_imports, "execute_run", new=execute),
    ):
        response = await degiro_imports.confirm_import(
            "run", degiro_imports.ConfirmRequest(), SimpleNamespace(), auth, db
        )
    assert response.status == "completed" and connection.last_error is None


@pytest.mark.asyncio
async def test_degiro_delete_files_cleans_staging_and_retention_paths(
    tmp_path,
) -> None:
    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    run = _degiro_run(status="failed", retained=True)
    staged = tmp_path / "staged" / "file.csv"
    staged.parent.mkdir()
    staged.write_text("x")
    retained = tmp_path / "retained" / str(run.id)
    retained.mkdir(parents=True)
    (retained / "file.csv").write_text("x")
    connection = SimpleNamespace(
        id="conn",
        description=json.dumps({"watchfolder": str(tmp_path / "watch")}),
    )
    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(run)), flush=AsyncMock()
    )
    settings = SimpleNamespace(degiro_import_staging_directory=tmp_path)
    with (
        patch.object(
            degiro_imports,
            "_connection",
            new=AsyncMock(return_value=connection),
        ),
        patch.object(degiro_imports, "stage_paths", return_value=[staged]),
        patch.object(
            degiro_imports,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
    ):
        response = await degiro_imports.delete_import_files(
            "run", SimpleNamespace(), auth, db
        )
    assert (
        response.retained is False
        and not staged.parent.exists()
        and not retained.exists()
    )


@pytest.mark.asyncio
async def test_degiro_preview_success_builds_auditable_run(tmp_path) -> None:
    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import degiro_imports

    class Result:
        def __init__(self, first_value=None, scalar_value=None):
            self.first_value, self.scalar_value = first_value, scalar_value

        def first(self):
            return self.first_value

        def scalar_one_or_none(self):
            return self.scalar_value

    connection = SimpleNamespace(id="conn", description="{}")
    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    staged = tmp_path / "staged" / "transactions.csv"
    staged.parent.mkdir()
    staged.write_text("x")
    preview = {
        "report_types": ["transactions"],
        "rows": 1,
        "skipped": 0,
        "warnings": [],
        "period_start": None,
        "period_end": None,
    }
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(None), Result(scalar_value=0)]),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )

    def add(run):
        run.created_at = datetime.now(UTC)
        run.created_count = 0
        run.updated_count = 0
        run.rejected_count = 0
        run.retained = False
        db.added = run

    db.add.side_effect = add
    with (
        patch.object(
            degiro_imports,
            "_connection",
            new=AsyncMock(return_value=connection),
        ),
        patch.object(
            degiro_imports,
            "get_container",
            return_value=SimpleNamespace(settings=SimpleNamespace()),
        ),
        patch.object(degiro_imports, "cleanup_expired_previews"),
        patch.object(
            degiro_imports,
            "stage_uploads",
            new=AsyncMock(
                return_value=([staged], ["transactions.csv"], ["hash"])
            ),
        ),
        patch.object(
            degiro_imports, "build_preview", new=AsyncMock(return_value=preview)
        ),
        patch.object(degiro_imports, "connector_options", return_value={}),
        patch.object(degiro_imports, "batch_hash", return_value="digest"),
    ):
        response = await degiro_imports.preview_import(
            SimpleNamespace(), "conn", [], auth, db
        )
    assert response.status == "previewed" and response.rows_total == 1


def test_destination_helpers_cover_validation_and_parity_shapes() -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import destinations

    assert (
        destinations._wealthfolio_provider_account_id({"providerAccountId": 7})
        == "7"
    )
    assert (
        destinations._wealthfolio_provider_account_id(
            {"provider_account_id": 8}
        )
        == "8"
    )
    assert destinations._wealthfolio_provider_account_id({}) == ""
    mappings = [
        SimpleNamespace(provider_account_id="a"),
        SimpleNamespace(provider_account_id=None),
    ]
    assert destinations._missing_wealthfolio_account_mappings(
        mappings, {"a"}
    ) == [mappings[1]]
    assert destinations._activity_parity_counts(
        [("one", None), ("two", datetime.now(UTC)), (None, None)],
        [{"sourceRecordId": "one"}, {"externalTransactionId": "three"}],
    ) == (1, 0, 1)
    assert destinations._parity_summary(
        "ok", {"remote_accounts": 2, "bogus": 9, "canonical_activities": -1}
    ) == {
        "status": "ok",
        "counts": {"remote_accounts": 2, "canonical_activities": 0},
    }
    assert (
        destinations._safe_url("https://example.test/api/")
        == "https://example.test/api"
    )
    assert (
        destinations._safe_url("http://127.0.0.1:8080/")
        == "http://127.0.0.1:8080"
    )
    for value in [None, "ftp://example.test", "http://example.test"]:
        with pytest.raises(HTTPException):
            destinations._safe_url(value)
    body = destinations.TargetCreate(
        target_type="jupyter", display_name="J", datasets=["accounts"]
    )
    destinations._validate_body(body, "jupyter")
    with pytest.raises(HTTPException):
        destinations._validate_body(
            destinations.TargetCreate(
                target_type="jupyter",
                display_name="J",
                configuration={"api_token": "x"},
            ),
            "jupyter",
        )
    with pytest.raises(HTTPException):
        destinations._validate_body(
            destinations.TargetCreate(
                target_type="jupyter", display_name="J", datasets=["unknown"]
            ),
            "jupyter",
        )
    preview = destinations._actual_account_mapping_preview(
        [("a", "Checking"), ("b", "New")],
        [{"id": "remote-a", "name": "Checking", "offbudget": True}],
        default_off_budget=False,
    )
    assert (
        preview[0]["action"] == "use_existing"
        and preview[1]["off_budget"] is False
    )


@pytest.mark.asyncio
async def test_destination_account_scopes_and_target_lookup_errors() -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import destinations

    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, 1]),
        scalars=AsyncMock(return_value=["a", "b"]),
    )
    with pytest.raises(HTTPException) as missing:
        await destinations._target(db, "tenant", "missing")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException):
        await destinations._validate_accounts(db, "tenant", ["a", "a"])
    with pytest.raises(HTTPException):
        await destinations._validate_accounts(db, "tenant", ["a", "b"])
    await destinations._validate_accounts(db, "tenant", [])
    assert await destinations._jupyter_account_scope(db, "tenant", ["x"]) == [
        "x"
    ]
    assert await destinations._jupyter_account_scope(db, "tenant", []) == [
        "a",
        "b",
    ]


@pytest.mark.asyncio
async def test_destination_probe_covers_non_wealthfolio_adapters() -> None:
    import finance_sync.api.v1.destinations as destinations
    from finance_sync.api.v1.destinations import test_target

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get_accounts(self):
            return []

        async def about(self):
            return {}

        async def health(self):
            return {}

        async def login(self):
            return None

    modules = [
        ("actual-budget", "ActualBudgetClient", "get_accounts"),
        ("firefly", "FireflyClient", "about"),
        ("ghostfolio", "GhostfolioClient", "health"),
        ("investbrain", "InvestBrainClient", "health"),
        ("securo", "SecuroClient", "login"),
    ]
    auth = SimpleNamespace(tenant_id="tenant")
    request = SimpleNamespace()
    settings = SimpleNamespace(destination_remote_probe_enabled=True)
    with (
        patch.object(
            destinations,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
        patch.object(
            destinations,
            "decrypt_credential",
            return_value='{"password":"p","access_token":"t"}',
        ),
        patch.object(destinations, "record_destination_probe"),
        patch.object(destinations, "_target", new=AsyncMock()),
        patch.object(
            destinations, "_safe_url", return_value="https://remote.test"
        ),
    ):
        for target_type, class_name, _method in modules:
            row = SimpleNamespace(
                id=target_type,
                target_type=target_type,
                configuration={},
                encrypted_secret=b"secret",
                secret_nonce=b"nonce",
                last_health_status=None,
                last_health_error=None,
                last_parity_summary={},
                last_checked_at=None,
            )
            destinations._target.return_value = row
            fake = Client()
            module_name = (
                "actual_budget"
                if target_type == "actual-budget"
                else target_type
            )
            module = __import__(
                f"finance_sync.exporter.{module_name}.client",
                fromlist=[class_name],
            )
            with patch.object(module, class_name, return_value=fake):
                response = await test_target(
                    target_type,
                    request,
                    auth,
                    None,
                    SimpleNamespace(flush=AsyncMock()),
                )
            assert response.status == "ready"

        degraded_row = SimpleNamespace(
            id="wealthfolio",
            target_type="wealthfolio",
            configuration={},
            encrypted_secret=b"secret",
            secret_nonce=b"nonce",
            last_health_status=None,
            last_health_error=None,
            last_parity_summary={},
            last_checked_at=None,
        )
        destinations._target.return_value = degraded_row
        with (
            patch.object(
                destinations,
                "probe_wealthfolio_destination",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        status="unavailable", reason="offline"
                    )
                ),
            ),
            patch(
                "finance_sync.exporter.wealthfolio.client.WealthfolioClient"
            ) as wf,
        ):
            wf.return_value.__aenter__ = AsyncMock(return_value=wf.return_value)
            wf.return_value.__aexit__ = AsyncMock(return_value=None)
            response = await test_target(
                "wealthfolio",
                request,
                auth,
                None,
                SimpleNamespace(flush=AsyncMock()),
            )
        assert response.status == "unavailable"


@pytest.mark.asyncio
async def test_connector_health_refresh_handles_success_failure_and_errors() -> (
    None
):
    import finance_sync.api.v1.connectors_config as config

    cred = SimpleNamespace(
        id="c",
        provider_key="bunq",
        encrypted_payload=b"x",
        nonce=b"n",
        description='{"region":"eu"}',
        last_error=None,
        last_error_category=None,
        last_test_at=None,
        last_test_status=None,
    )
    auth = SimpleNamespace(tenant_id="tenant")
    request = SimpleNamespace()
    db = SimpleNamespace(flush=AsyncMock())
    overview = SimpleNamespace(connection_id="c")
    health_service = SimpleNamespace(
        get_overview=AsyncMock(return_value=[overview])
    )
    connector = SimpleNamespace(
        health=AsyncMock(
            return_value=SimpleNamespace(healthy=True, message="ok")
        )
    )
    registry = SimpleNamespace(get_connector=MagicMock(return_value=connector))
    settings = SimpleNamespace()
    with (
        patch.object(
            config, "_load_tenant_credential", new=AsyncMock(return_value=cred)
        ),
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
        patch.object(
            config, "decrypt_credential", return_value='{"token":"secret"}'
        ),
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(
            config, "ProviderHealthService", return_value=health_service
        ),
    ):
        result = await config.get_connection_health(
            "c", request, True, auth, db
        )
    assert result.connection_id == "c" and cred.last_test_status == "passed"

    connector.health = AsyncMock(
        return_value=SimpleNamespace(healthy=False, message="down")
    )
    with (
        patch.object(
            config, "_load_tenant_credential", new=AsyncMock(return_value=cred)
        ),
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
        patch.object(
            config, "decrypt_credential", return_value='{"token":"secret"}'
        ),
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(
            config, "ProviderHealthService", return_value=health_service
        ),
    ):
        await config.get_connection_health("c", request, True, auth, db)
    assert cred.last_test_status == "failed"

    registry.get_connector.side_effect = RuntimeError("connector down")
    with (
        patch.object(
            config, "_load_tenant_credential", new=AsyncMock(return_value=cred)
        ),
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
        patch.object(
            config, "decrypt_credential", side_effect=ValueError("bad")
        ),
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(
            config, "ProviderHealthService", return_value=health_service
        ),
    ):
        await config.get_connection_health("c", request, True, auth, db)
    assert cred.last_error_category == "unknown"


@pytest.mark.asyncio
async def test_wealthfolio_client_auth_and_url_resolution_error_paths() -> None:
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioAuthError,
        WealthfolioClient,
        WealthfolioClientConfig,
        resolve_wealthfolio_server_url,
    )

    assert (
        resolve_wealthfolio_server_url(
            "https://remote.test", running_in_container=True
        )
        == "https://remote.test"
    )
    assert (
        resolve_wealthfolio_server_url(
            "http://localhost:3000/app", running_in_container=True
        )
        == "http://host.docker.internal:3000/app"
    )
    assert (
        resolve_wealthfolio_server_url(
            "http://localhost", running_in_container=False
        )
        == "http://localhost"
    )

    class Response:
        status_code = 401

        def json(self):
            return {"message": "bad password"}

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(401, json={"message": "bad password"})

    client = WealthfolioClient(
        WealthfolioClientConfig(base_url="https://wf.test", password="x"),
        Transport(),
    )
    with pytest.raises(WealthfolioAuthError, match="bad password"):
        await client.authenticate()
    assert not client.is_authenticated
    await client.close()


@pytest.mark.asyncio
async def test_wealthfolio_ensure_account_updates_only_changed_projection() -> (
    None
):
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
        WealthfolioClientConfig,
    )

    client = WealthfolioClient(
        WealthfolioClientConfig(base_url="https://wf.test", password="x"),
        httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    client._is_authenticated = True
    with pytest.raises(ValueError):
        await client.create_account(
            name="A",
            currency="EUR",
            provider_account_id="a",
            account_type="unknown",
        )
    with pytest.raises(ValueError):
        await client.create_account(
            name="A",
            currency="EUR",
            provider_account_id="a",
            tracking_mode="unknown",
        )
    client.get_accounts = AsyncMock(return_value=[])
    client.create_account = AsyncMock(return_value={"id": "new"})
    assert await client.ensure_account(
        name="A", currency="EUR", provider_account_id="a"
    ) == {"id": "new"}
    existing = {
        "id": "old",
        "provider": "FINANCE_SYNC",
        "providerAccountId": "a",
        "name": "Old",
        "currency": "USD",
        "accountType": "CASH",
        "trackingMode": "HOLDINGS",
        "isActive": False,
        "isArchived": True,
        "group": "Cash",
    }
    client.get_accounts = AsyncMock(return_value=[existing])
    client.update_account = AsyncMock(return_value={"id": "updated"})
    assert await client.ensure_account(
        name="A", currency="EUR", provider_account_id="a"
    ) == {"id": "updated"}
    tracking_only = {
        "id": "old",
        "provider": "FINANCE_SYNC",
        "providerAccountId": "a",
        "name": "A",
        "currency": "EUR",
        "accountType": "SECURITIES",
        "trackingMode": "HOLDINGS",
        "isActive": True,
        "isArchived": False,
        "group": "Investments",
    }
    client.get_accounts = AsyncMock(return_value=[tracking_only])
    client.update_account_tracking_mode = AsyncMock(
        return_value={"id": "tracking"}
    )
    assert await client.ensure_account(
        name="A", currency="EUR", provider_account_id="a"
    ) == {"id": "tracking"}
    await client.close()


@pytest.mark.asyncio
async def test_wealthfolio_activity_cleanup_respects_source_and_preserved_comments() -> (
    None
):
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
        WealthfolioClientConfig,
    )

    client = WealthfolioClient(
        WealthfolioClientConfig(base_url="https://wf.test", password="x"),
        httpx.MockTransport(lambda request: httpx.Response(204)),
    )
    client._is_authenticated = True
    rows = [
        {"id": "keep", "comment": "finance-sync ID:keep"},
        {"id": "drop", "comment": "finance-sync ID:drop"},
        {"id": "manual", "comment": "user-note"},
        {"comment": "no-id"},
    ]
    client.get_all_activities = AsyncMock(return_value=rows)
    client.delete_activity = AsyncMock()
    assert (
        await client.delete_activities_not_in(
            "a", {"keep"}, preserved_comment_prefixes=("user-",)
        )
        == 1
    )
    client.get_all_activities = AsyncMock(return_value=rows)
    assert (
        await client.delete_cash_reconciliation_activities("a", "finance-sync")
        == 2
    )
    client.get_all_activities = AsyncMock(return_value=rows)
    assert (
        await client.delete_activities_by_comment_prefix("a", "finance-sync")
        == 2
    )
    await client.close()


@pytest.mark.asyncio
async def test_wealthfolio_pagination_and_408_retry_paths() -> None:
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
        WealthfolioClientConfig,
    )

    client = WealthfolioClient(
        WealthfolioClientConfig(
            base_url="https://wf.test",
            password="x",
            retry_408=True,
            retry_408_attempts=2,
            retry_408_base_delay=0,
        ),
        httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    client._is_authenticated = True
    pages = [
        {"data": [{"id": "1"}] * 1000, "meta": {"totalRowCount": 2000}},
        {"activities": [{"id": "2"}]},
    ]
    client.search_activities = AsyncMock(side_effect=pages)
    assert len(await client.get_all_activities("a")) == 1001
    client.search_activities = AsyncMock(return_value={"data": []})
    assert await client.get_all_activities("a") == []
    responses = [httpx.Response(408), httpx.Response(200, json={"ok": True})]
    client._client.post = AsyncMock(side_effect=responses)
    response = await client._post_with_408_retry("/slow", json={})
    assert response.status_code == 200
    await client.close()


@pytest.mark.asyncio
async def test_wealthfolio_owned_account_and_quote_deduplication_paths() -> (
    None
):
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
        WealthfolioClientConfig,
    )

    client = WealthfolioClient(
        WealthfolioClientConfig(base_url="https://wf.test", password="x"),
        httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    client._is_authenticated = True
    client.get_accounts = AsyncMock(
        return_value=[
            {"id": "keep", "providerAccountId": "a"},
            {"id": "duplicate", "providerAccountId": "a"},
            {"id": "stale", "providerAccountId": "b"},
            {"providerAccountId": "c"},
        ]
    )
    client.delete_account = AsyncMock()
    assert await client.delete_accounts_not_owned_by_finance_sync({"a"}) == 2
    assert client.delete_account.await_count == 2
    client.get_quote_history = AsyncMock(
        return_value=[
            {
                "id": "old",
                "source": "FINANCE_SYNC",
                "timestamp": "2026-01-01T00:00:00",
            },
            {
                "id": "other",
                "source": "MANUAL",
                "timestamp": "2026-01-01T00:00:00",
            },
        ]
    )
    client.delete_quote = AsyncMock()
    response = await client.upsert_quote(
        "asset", {"timestamp": "2026-01-01T12:00:00", "close": "1"}
    )
    assert response is None
    client.delete_quote.assert_awaited_once_with("old")
    await client.close()


def test_file_upload_detection_covers_provider_markers_and_invalid_files(
    tmp_path,
) -> None:
    from finance_sync.api.v1.file_uploads import (
        _credential_label,
        _csv_mapping,
        _detect,
        _inspect_path,
    )

    csv_path = tmp_path / "transactions.csv"
    csv_path.write_text("Date;Amount;Description\n2026-01-01;10;Coffee\n")
    markers, evidence = _inspect_path(csv_path)
    assert "csv_content" in markers and evidence
    degiro = tmp_path / "degiro_portfolio.csv"
    degiro.write_text("Order ID;Value date;Local value\n1;2026-01-01;10\n")
    assert "degiro_content" in _inspect_path(degiro)[0]
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not-json")
    assert _inspect_path(bad_json)[0] == set()
    assert _detect({"saxo_filename"})[0] == "saxo_investor"
    assert _detect({"degiro_filename"})[0] == "degiro_pension"
    assert _detect({"manual_expense_content"})[0] == "manual_expense"
    assert _detect({"csv_content"})[0] == "csv_import"
    assert _detect(set())[0] is None
    assert _credential_label('{"_label":"My file"}', "csv_import") == "My file"
    assert _credential_label("bad", "csv_import") == "CSV import"
    assert _credential_label(None, "other") == "other"
    mapping_path = tmp_path / "mapping.csv"
    mapping_path.write_text("Datum;Omschrijving;Bedrag\n")
    assert _csv_mapping(mapping_path) == {
        "date": "Datum",
        "description": "Omschrijving",
        "amount": "Bedrag",
    }
    from zipfile import ZipFile

    xlsx_path = tmp_path / "positions.xlsx"
    with ZipFile(xlsx_path, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml", "<c>ISIN quantity slotkoers</c>"
        )
    xlsx_markers, xlsx_evidence = _inspect_path(xlsx_path)
    assert (
        "broker_xlsx_content" in xlsx_markers and "saxo_content" in xlsx_markers
    )
    assert xlsx_evidence
    broken_xlsx = tmp_path / "broken.xlsx"
    broken_xlsx.write_bytes(b"not-a-zip")
    assert _inspect_path(broken_xlsx) == (set(), [])


@pytest.mark.asyncio
async def test_generic_file_import_success_and_manual_json_validation(
    tmp_path,
) -> None:
    import finance_sync.api.v1.file_uploads as uploads
    from finance_sync.models.enums import SyncRunStatus

    auth = SimpleNamespace(tenant_id="tenant")
    row = SimpleNamespace(
        id="conn",
        provider_key="csv_import",
        description="{}",
        last_success_at=None,
        last_attempt_at=None,
        last_error=None,
        last_error_category=None,
    )
    result = SimpleNamespace(
        status=SyncRunStatus.COMPLETED,
        accounts_synced=0,
        transactions_synced=3,
        holdings_synced=0,
        error_message=None,
    )
    orchestrator = SimpleNamespace(run_sync=AsyncMock(return_value=result))
    settings = SimpleNamespace()
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=row),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    staged = tmp_path / "staged" / "expenses.csv"
    staged.parent.mkdir()
    staged.write_text("Date,Description,Amount\n2026-01-01,Coffee,2\n")
    with (
        patch.object(
            uploads,
            "get_container",
            return_value=SimpleNamespace(
                settings=settings, session_factory=MagicMock()
            ),
        ),
        patch.object(
            uploads,
            "stage_uploads",
            new=AsyncMock(return_value=([staged], ["expenses.csv"], ["hash"])),
        ),
        patch.object(uploads, "SyncOrchestrator", return_value=orchestrator),
    ):
        response = await uploads.import_generic_file(
            SimpleNamespace(), "conn", [], auth, db
        )
    assert response["status"] == "completed" and row.last_success_at is not None

    manual = SimpleNamespace(
        id="manual",
        provider_key="manual_expense",
        description="{}",
        last_success_at=None,
        last_attempt_at=None,
        last_error=None,
        last_error_category=None,
    )
    db.scalar.return_value = manual
    staged_json = tmp_path / "manual" / "expenses.csv"
    staged_json.parent.mkdir()
    staged_json.write_text("not json")
    with (
        patch.object(
            uploads,
            "get_container",
            return_value=SimpleNamespace(
                settings=settings, session_factory=MagicMock()
            ),
        ),
        patch.object(
            uploads,
            "stage_uploads",
            new=AsyncMock(
                return_value=([staged_json], ["expenses.csv"], ["hash"])
            ),
        ),
    ):
        with pytest.raises(Exception):
            await uploads.import_generic_file(
                SimpleNamespace(), "manual", [], auth, db
            )


@pytest.mark.asyncio
async def test_file_dispatch_routes_each_supported_provider() -> None:
    import finance_sync.api.v1.file_uploads as uploads

    auth = SimpleNamespace(tenant_id="tenant")
    db = MagicMock()
    request = SimpleNamespace()
    for provider, handler in [
        ("degiro_pension", "preview_degiro_import"),
        ("saxo_investor", "import_saxo_files"),
        ("csv_import", "import_generic_file"),
        ("manual_expense", "import_generic_file"),
    ]:
        with patch.object(
            uploads, handler, new=AsyncMock(return_value={"provider": provider})
        ) as mocked:
            result = await uploads.dispatch_file_import(
                request, provider, "c", [], auth, db
            )
        assert result["provider"] == provider  # pyright: ignore[reportIndexIssue]
        mocked.assert_awaited_once()
    with pytest.raises(Exception):
        await uploads.dispatch_file_import(
            request, "unknown", "c", [], auth, db
        )


def test_connector_config_helpers_cover_secret_and_release_shapes() -> None:
    from finance_sync.api.v1 import connectors_config

    assert connectors_config._account_enumeration_error_is_fatal("bunq")
    assert not connectors_config._account_enumeration_error_is_fatal(
        "trading212"
    )
    assert (
        connectors_config._get_registry() is connectors_config._get_registry()
    )
    row = SimpleNamespace(
        id="c",
        provider_key="csv_import",
        encrypted_payload=b"",
        nonce=b"",
        description='{"_label":"My CSV","region":"eu"}',
        status="active",
        selected_accounts=[],
        last_attempt_at=None,
        last_success_at=None,
        last_error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    response = connectors_config._credential_response(row)
    assert response.description == "My CSV" and response.is_configured is True
    row.description = "not-json"
    assert connectors_config._credential_response(row).description
    release = SimpleNamespace(
        id="r",
        provider_key="bunq",
        version="1",
        status="certified",
        previous_version=None,
        certification_status="certified",
        certification_commit=None,
        compatibility_status="compatible",
        canary_status="passed",
        capabilities=None,
        reason_code=None,
        enabled_at=None,
        disabled_at=None,
    )
    assert connectors_config._release_response(release).capabilities == []
    with patch.object(
        connectors_config,
        "decrypt_credential",
        return_value='{"token":"secret","number":3}',
    ):
        assert connectors_config._credential_secrets(
            SimpleNamespace(encrypted_payload=b"cipher", nonce=b"nonce"),
            SimpleNamespace(),
        ) == ["secret"]
    with patch.object(
        connectors_config, "decrypt_credential", side_effect=ValueError("bad")
    ):
        assert (
            connectors_config._credential_secrets(
                SimpleNamespace(encrypted_payload=b"cipher", nonce=b"nonce"),
                SimpleNamespace(),
            )
            == []
        )


@pytest.mark.asyncio
async def test_saxo_import_connection_and_upload_validation_errors(
    tmp_path,
) -> None:
    from fastapi import HTTPException

    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import saxo_imports

    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one_or_none(self) -> object:
            return self.value

    missing_db = SimpleNamespace(execute=AsyncMock(return_value=Result(None)))
    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    with pytest.raises(HTTPException) as exc:
        await saxo_imports._connection(missing_db, "missing", "tenant")
    assert exc.value.status_code == 404

    connection = SimpleNamespace(id="conn", description="{}")
    settings = SimpleNamespace()
    container = SimpleNamespace(settings=settings)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(connection)),
        scalar=AsyncMock(return_value=0),
        rollback=AsyncMock(),
        commit=AsyncMock(),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    request = SimpleNamespace()
    with (
        patch.object(
            saxo_imports, "_connection", new=AsyncMock(return_value=connection)
        ),
        patch.object(saxo_imports, "get_container", return_value=container),
        patch.object(saxo_imports, "report_connector_failure", new=AsyncMock()),
        patch.object(
            saxo_imports,
            "stage_uploads",
            new=AsyncMock(
                return_value=([tmp_path / "only.csv"], ["only.csv"], ["hash"])
            ),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await saxo_imports.import_files(request, "conn", [], auth, db)
    assert exc.value.status_code == 422

    from finance_sync.models.enums import SyncRunStatus

    staged_dir = tmp_path / "next" / "staged"
    staged_dir.mkdir(parents=True)
    first, second = (
        staged_dir / "positions.xlsx",
        staged_dir / "transactions.xlsx",
    )
    first.write_bytes(b"x")
    second.write_bytes(b"y")
    db.scalar.return_value = 0
    result = SimpleNamespace(
        status=SyncRunStatus.COMPLETED,
        accounts_synced=1,
        transactions_synced=2,
        holdings_synced=3,
        unresolved_securities=0,
    )
    fake_orchestrator = SimpleNamespace(run_sync=AsyncMock(return_value=result))
    with (
        patch.object(
            saxo_imports, "_connection", new=AsyncMock(return_value=connection)
        ),
        patch.object(
            saxo_imports,
            "get_container",
            return_value=SimpleNamespace(
                settings=settings, session_factory=MagicMock()
            ),
        ),
        patch.object(
            saxo_imports,
            "stage_uploads",
            new=AsyncMock(
                return_value=(
                    [first, second],
                    ["positions.xlsx", "transactions.xlsx"],
                    ["h1", "h2"],
                )
            ),
        ),
        patch.object(saxo_imports, "SaxoInvestorConnector") as validator,
        patch.object(
            saxo_imports, "SyncOrchestrator", return_value=fake_orchestrator
        ),
        patch.object(saxo_imports, "report_connector_failure", new=AsyncMock()),
    ):
        validator.return_value.authenticate = AsyncMock()
        validator.return_value.export_roles = ["positions", "transactions"]
        response = await saxo_imports.import_files(
            request, "conn", [], auth, db
        )
    assert response.accounts == 1 and response.holdings == 3

    staged_dir = tmp_path / "failed" / "staged"
    staged_dir.mkdir(parents=True)
    first, second = (
        staged_dir / "positions.xlsx",
        staged_dir / "transactions.xlsx",
    )
    first.write_bytes(b"x")
    second.write_bytes(b"y")
    failed_result = SimpleNamespace(
        status=SyncRunStatus.FAILED,
        accounts_synced=0,
        transactions_synced=0,
        holdings_synced=0,
        unresolved_securities=1,
        error_message="parser failed",
    )
    failed_orchestrator = SimpleNamespace(
        run_sync=AsyncMock(return_value=failed_result)
    )
    with (
        patch.object(
            saxo_imports, "_connection", new=AsyncMock(return_value=connection)
        ),
        patch.object(
            saxo_imports,
            "get_container",
            return_value=SimpleNamespace(
                settings=settings, session_factory=MagicMock()
            ),
        ),
        patch.object(
            saxo_imports,
            "stage_uploads",
            new=AsyncMock(
                return_value=(
                    [first, second],
                    ["positions.xlsx", "transactions.xlsx"],
                    ["h1", "h2"],
                )
            ),
        ),
        patch.object(saxo_imports, "SaxoInvestorConnector") as validator,
        patch.object(
            saxo_imports, "SyncOrchestrator", return_value=failed_orchestrator
        ),
        patch.object(saxo_imports, "report_connector_failure", new=AsyncMock()),
    ):
        validator.return_value.authenticate = AsyncMock()
        validator.return_value.export_roles = ["positions", "transactions"]
        with pytest.raises(HTTPException):
            await saxo_imports.import_files(request, "conn", [], auth, db)
    assert db.commit.await_count >= 2

    db.rollback.reset_mock()
    with (
        patch.object(
            saxo_imports, "_connection", new=AsyncMock(return_value=connection)
        ),
        patch.object(saxo_imports, "get_container", return_value=container),
        patch.object(saxo_imports, "report_connector_failure", new=AsyncMock()),
        patch.object(
            saxo_imports,
            "stage_uploads",
            new=AsyncMock(
                return_value=([tmp_path / "a.xlsx"], ["a.xlsx"], ["hash"])
            ),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await saxo_imports.import_files(request, "conn", [], auth, db)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_non_production_seed_is_idempotent_and_builds_normalized_rows() -> (
    None
):
    from finance_sync.services.non_production_seed import (
        seed_non_production_dataset,
    )

    class Bind:
        dialect = SimpleNamespace(name="sqlite")

    session = SimpleNamespace(
        get_bind=MagicMock(return_value=Bind()),
        scalar=AsyncMock(side_effect=[None, "existing"]),
        scalars=AsyncMock(return_value=[]),
        add_all=MagicMock(),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    assert await seed_non_production_dataset(session, "tenant", "owner") is True
    assert session.add_all.call_count >= 3
    session.commit.assert_awaited_once()
    assert (
        await seed_non_production_dataset(session, "tenant", "owner") is False
    )


def test_sync_report_sorts_counts_and_accumulates() -> None:
    from finance_sync.services.sync_reporting import SyncReport

    report = SyncReport()
    report.record("updated", 2)
    report.record("created")
    report.record("updated")
    assert report.as_dict() == {"created": 1, "updated": 3}


def test_connector_capability_normalization_skips_invalid_declarations() -> (
    None
):
    from finance_sync.connectors.capabilities import normalize_capabilities

    assert normalize_capabilities("invalid") == {}
    result = normalize_capabilities(
        {
            "cash": {"availability": "complete", "notes": "safe"},
            "": {"availability": "complete"},
            "bad": {"availability": "not-a-valid-value"},
            "partial": "invalid-shape",
        }
    )
    assert set(result) == {"cash"}


@pytest.mark.asyncio
async def test_allocation_endpoint_projects_requested_scope_and_currency() -> (
    None
):
    from finance_sync.api.v1 import allocation

    service = SimpleNamespace(
        get_allocation=AsyncMock(
            return_value=SimpleNamespace(
                model_dump=lambda: {"items": [], "meta": {}}
            )
        )
    )
    auth = SimpleNamespace(tenant_id="tenant")
    with patch.object(allocation, "_get_service", return_value=service):
        result = await allocation.get_allocation(
            auth,
            SimpleNamespace(),
            SimpleNamespace(),
            target_currency="EUR",
            account_id="a",
        )
    assert result["items"] == []
    service.get_allocation.assert_awaited_once_with(
        tenant_id="tenant", target_currency="EUR", account_id="a"
    )


def test_worker_module_entrypoint_delegates_to_main() -> None:
    import runpy
    import finance_sync.worker as worker

    with patch.object(worker, "main") as main:
        runpy.run_module("finance_sync.worker.__main__", run_name="__main__")
    main.assert_called_once_with()


def test_spending_classification_prefers_overrides_and_merges_safely() -> None:
    from finance_sync.connectors.models import CanonicalTransactionData
    from finance_sync.services.spending_classification import (
        MerchantMapping,
        merge_destination_enrichment,
        normalize_merchant_key,
        suggest_category,
    )

    assert normalize_merchant_key(None) is None
    assert normalize_merchant_key("  ACME, Inc. ", "NL") == "acme inc:nl"
    base = CanonicalTransactionData(
        provider_key="test",
        external_account_id="a",
        external_transaction_id="x",
        amount="1",
        currency_code="EUR",
        occurred_at=datetime.now(UTC),
        transaction_type="payment",
        merchant_name="ACME, Inc.",
        merchant_country="NL",
        classification_override="custom",
    )
    assert suggest_category(base).source == "user_override"
    base.classification_override = None
    mapping = {
        "acme inc:nl": MerchantMapping("acme inc:nl", "Acme", "office", "tax")
    }
    assert suggest_category(base, mapping).taxonomy == "tax"
    base.merchant_name = "Other"
    base.merchant_category_code = "5812"
    assert suggest_category(base, mapping).source == "mcc"
    base.merchant_category_code = None
    base.cashflow_suggestion = None
    assert suggest_category(base, mapping) is None
    merged = merge_destination_enrichment(
        {"category": "user", "amount": 1},
        {"category": "remote", "amount": 2, "note": None},
        {"category": "override"},
    )
    assert merged == {"category": "override", "amount": 2}


def test_datamart_schemas_validate_unique_governance_fields() -> None:
    from pydantic import ValidationError
    from finance_sync.api.v1.datamarts import (
        ConsumerCreate,
        DataMartCreate,
        GrantCreate,
        _consumer_response,
        _mart_response,
    )

    valid = DataMartCreate(
        key="spending",
        display_name="Spending",
        dataset="transactions",
        schema_version="pfc/1.0",
        fields=["id"],
        delivery_method="pull_api",
    )
    assert valid.fields == ["id"]
    for kwargs in [
        {"fields": ["id", "id"]},
        {"fields": [""]},
    ]:
        with pytest.raises(ValidationError):
            DataMartCreate(
                key="spending",
                display_name="Spending",
                dataset="transactions",
                schema_version="pfc/1.0",
                fields=kwargs["fields"],
                delivery_method="pull_api",
            )
    with pytest.raises(ValidationError):
        DataMartCreate(
            key="spending",
            display_name="Spending",
            dataset="transactions",
            schema_version="pfc/1.0",
            fields=["id"],
            delivery_method="unknown",
        )
    with pytest.raises(ValidationError):
        GrantCreate(consumer_id="c", datamart_id="m", allowed_fields=["x", "x"])
    assert (
        ConsumerCreate(key="consumer", display_name="Consumer").display_name
        == "Consumer"
    )
    assert (
        _consumer_response(
            SimpleNamespace(
                id="c",
                key="consumer",
                display_name="Consumer",
                api_key_id=None,
                is_active=True,
            )
        ).api_key_id
        is None
    )
    assert (
        _mart_response(
            SimpleNamespace(
                id="m",
                key="spending",
                display_name="Spending",
                dataset="transactions",
                schema_version="pfc/1.0",
                fields=None,
                delivery_method="pull_api",
                delivery_config=None,
                is_active=True,
            )
        ).fields
        == []
    )


@pytest.mark.asyncio
async def test_spending_mutation_endpoints_cover_idempotency_and_corrections() -> (
    None
):
    from fastapi import HTTPException

    from finance_sync.api.deps.auth import AuthContext
    from finance_sync.api.v1 import spending
    from finance_sync.models.enums import TransactionType

    auth = AuthContext(
        api_key_result=SimpleNamespace(
            tenant_id="tenant", api_key=SimpleNamespace(id="principal")
        )
    )
    tx = SimpleNamespace(
        id="tx",
        revision=2,
        transaction_type=None,
        unit_price=None,
        classification_override=None,
        provider_metadata_contract=None,
    )
    db = SimpleNamespace(
        add=MagicMock(), commit=AsyncMock(), scalar=AsyncMock()
    )
    with patch.object(
        spending, "_load_transaction", new=AsyncMock(return_value=tx)
    ):
        with pytest.raises(HTTPException):
            await spending.correct_data_quality_transaction(
                "tx", spending.DataQualityCorrectionRequest(), auth, db
            )
        result = await spending.correct_data_quality_transaction(
            "tx",
            spending.DataQualityCorrectionRequest(
                transaction_type=TransactionType.PAYMENT, unit_price="12.50"
            ),
            auth,
            db,
        )
        assert result["changes"] == {
            "transaction_type": "payment",
            "unit_price": "12.50",
        }
        override = await spending.create_spending_override(
            "tx",
            spending.SpendingOverrideRequest(
                field_name="classification_override", value="food"
            ),
            auth,
            db,
        )
        assert override["field_name"] == "classification_override"
        await spending.create_spending_override(
            "tx",
            spending.SpendingOverrideRequest(field_name="note", value=1),
            auth,
            db,
        )
        db.scalar.return_value = SimpleNamespace(id="existing")
        existing_split = await spending.create_spending_split(
            "tx",
            spending.SpendingSplitRequest(
                idempotency_key="k", amount="1", currency_code="eur"
            ),
            auth,
            db,
        )
        assert existing_split["id"] == "existing"
        db.scalar.return_value = None
        new_split = await spending.create_spending_split(
            "tx",
            spending.SpendingSplitRequest(
                idempotency_key="k2",
                amount="1",
                currency_code="eur",
                percentage="50",
            ),
            auth,
            db,
        )
        assert new_split["transaction_id"] == "tx"
        db.scalar.return_value = SimpleNamespace(id="event-existing")
        assert (
            await spending.create_spending_event(
                "tx",
                spending.SpendingEventRequest(
                    event_type="note", idempotency_key="e"
                ),
                auth,
                db,
            )
        )["id"] == "event-existing"
        db.scalar.return_value = None
        assert (
            await spending.create_spending_event(
                "tx",
                spending.SpendingEventRequest(
                    event_type="note", idempotency_key="e2", payload={"x": 1}
                ),
                auth,
                db,
            )
        )["transaction_id"] == "tx"


@pytest.mark.asyncio
async def test_destination_catalog_and_listing_cover_schedule_join() -> None:
    from finance_sync.api.v1.destinations import (
        list_destination_capabilities,
        list_targets,
        list_types,
    )

    types = await list_types()
    assert {item["key"] for item in types} >= {
        "wealthfolio",
        "actual-budget",
        "jupyter",
    }
    capabilities = await list_destination_capabilities()
    assert (
        "wealthfolio" in capabilities
        and "accounts" in capabilities["wealthfolio"]
    )

    target = SimpleNamespace(
        id="target",
        tenant_id="tenant",
        target_type="jupyter",
        display_name="Jupyter",
        status="active",
        version=1,
        configuration={},
        selected_account_ids=[],
        datasets=[],
        encrypted_secret=None,
        secret_nonce=None,
        jupyter_api_key_id=None,
        schedule_id=None,
        last_health_status=None,
        last_health_error=None,
        last_parity_summary={},
        last_checked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    scalar_result = SimpleNamespace(all=lambda: [target])
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: scalar_result)
        )
    )
    rows = await list_targets(SimpleNamespace(tenant_id="tenant"), db)
    assert rows[0].display_name == "Jupyter"


@pytest.mark.asyncio
async def test_destination_reconciliation_and_disabled_probe_paths() -> None:
    import finance_sync.api.v1.destinations as destinations
    from finance_sync.api.v1.destinations import (
        ReconciliationRequest,
        reconcile_target,
        test_target,
    )

    target = SimpleNamespace(
        id="target",
        tenant_id="tenant",
        target_type="jupyter",
        display_name="Jupyter",
        status="active",
        version=1,
        configuration={},
        selected_account_ids=[],
        datasets=[],
        encrypted_secret=None,
        secret_nonce=None,
        jupyter_api_key_id=None,
        last_health_status=None,
        last_health_error=None,
        last_parity_summary={},
        last_checked_at=None,
    )

    class Result:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def all(self):
            return self.rows

        def scalars(self):
            return self

    db = SimpleNamespace(
        scalar=AsyncMock(return_value=target),
        execute=AsyncMock(side_effect=[Result([("a",)]), Result([])]),
        flush=AsyncMock(),
    )
    result = await reconcile_target(
        "target",
        ReconciliationRequest(records=[]),
        SimpleNamespace(tenant_id="tenant"),
        db,
    )
    assert result["target_id"] == "target" and result["finding_count"] == 0
    settings = SimpleNamespace(destination_remote_probe_enabled=False)
    with patch.object(
        destinations,
        "get_container",
        return_value=SimpleNamespace(settings=settings),
    ):
        response = await test_target(
            "target", None, SimpleNamespace(tenant_id="tenant"), None, db
        )
    assert response.status == "disabled"
    assert target.last_health_status == "disabled"


@pytest.mark.asyncio
async def test_destination_update_and_preview_paths() -> None:
    import finance_sync.api.v1.destinations as destinations
    from finance_sync.api.v1.destinations import (
        TargetUpdate,
        preview_target,
        update_target,
    )

    now = datetime.now(UTC)
    row = SimpleNamespace(
        id="target",
        tenant_id="tenant",
        target_type="wealthfolio",
        display_name="Old",
        status="active",
        version=1,
        configuration={"server_url": "https://wf.test"},
        selected_account_ids=[],
        datasets=["accounts"],
        encrypted_secret=None,
        secret_nonce=None,
        jupyter_api_key_id=None,
        schedule_id=None,
        last_health_status=None,
        last_health_error=None,
        last_parity_summary={},
        last_checked_at=None,
        created_at=now,
        updated_at=now,
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=row), flush=AsyncMock())
    updated = await update_target(
        "target",
        TargetUpdate(display_name="New", datasets=["accounts"]),
        None,
        SimpleNamespace(tenant_id="tenant"),
        db,
    )
    assert updated.display_name == "New" and row.version == 2

    class Result:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def all(self):
            return self.rows

        def scalars(self):
            return self

    db.scalar = AsyncMock(return_value=row)
    db.execute = AsyncMock(
        side_effect=[Result([("a", "Checking")]), Result([])]
    )
    preview = await preview_target(
        "target", None, SimpleNamespace(tenant_id="tenant"), db
    )
    assert (
        preview.account_count == 1 and preview.accounts[0]["name"] == "Checking"
    )


@pytest.mark.asyncio
async def test_destination_jupyter_activation_pause_and_notebook_paths() -> (
    None
):
    import finance_sync.api.v1.destinations as destinations
    from finance_sync.api.v1.destinations import (
        activate_target,
        download_jupyter_notebook,
        pause_target,
    )

    now = datetime.now(UTC)
    row = SimpleNamespace(
        id="j",
        tenant_id="tenant",
        target_type="jupyter",
        display_name="Jupyter",
        status="active",
        version=1,
        configuration={},
        selected_account_ids=["a"],
        datasets=["accounts"],
        encrypted_secret=None,
        secret_nonce=None,
        jupyter_api_key_id=None,
        schedule_id=None,
        last_health_status=None,
        last_health_error=None,
        last_parity_summary={},
        last_checked_at=None,
        created_at=now,
        updated_at=now,
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=row), flush=AsyncMock(), add=MagicMock()
    )
    auth = SimpleNamespace(tenant_id="tenant", principal_id="user")
    activated = await activate_target("j", auth, db)
    assert (
        activated.target.status == "active"
        and activated.jupyter_bootstrap is not None
    )
    paused = await pause_target("j", auth, db)
    assert paused.status == "paused"
    notebook = await download_jupyter_notebook("j", auth, db)
    assert "read_dataset" in notebook.body.decode()


@pytest.mark.asyncio
async def test_file_upload_inspection_and_dispatch_rejects_unknown_provider(
    tmp_path,
) -> None:
    import finance_sync.api.v1.file_uploads as uploads
    from fastapi import HTTPException

    path = tmp_path / "expenses.json"
    path.write_text('{"expenses": []}')

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    db = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    settings = SimpleNamespace(is_staging=False)
    auth = SimpleNamespace(tenant_id="tenant")
    with (
        patch.object(
            uploads,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
        patch.object(
            uploads,
            "stage_uploads",
            new=AsyncMock(return_value=([path], [path.name], [])),
        ),
    ):
        result = await uploads.inspect_upload(None, [], auth, db)
    assert result["detected_provider"] == "manual_expense"
    with pytest.raises(HTTPException):
        await uploads.dispatch_file_import(
            None, "unknown", "connection", [], auth, db
        )


@pytest.mark.asyncio
async def test_connector_connection_test_handles_file_source_and_decrypt_failure() -> (
    None
):
    import finance_sync.api.v1.connectors_config as config
    from finance_sync.api.v1.connectors_config import test_connector_connection

    now = datetime.now(UTC)
    base = dict(
        id="connection",
        provider_key="saxo_investor",
        description="{}",
        encrypted_payload=None,
        nonce=None,
        status="active",
        selected_accounts=[],
        last_attempt_at=None,
        last_success_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
        last_test_at=None,
        last_test_status=None,
        last_test_error=None,
        credential_status=None,
        last_authenticated_at=None,
        expires_at=None,
        reauth_required_at=None,
        credential_version=1,
    )
    auth = SimpleNamespace(tenant_id="tenant", principal_id="user", user=None)
    db = SimpleNamespace(flush=AsyncMock())
    with (
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=SimpleNamespace()),
        ),
        patch.object(
            config,
            "_load_tenant_credential",
            new=AsyncMock(return_value=SimpleNamespace(**base)),
        ),
        patch.object(config, "log_connection_event", new=AsyncMock()),
    ):
        result = await test_connector_connection("connection", None, auth, db)
    assert result.success and "Upload" in result.message

    encrypted = SimpleNamespace(
        **{
            **base,
            "provider_key": "bunq",
            "encrypted_payload": b"cipher",
            "nonce": b"nonce",
        }
    )
    with (
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=SimpleNamespace()),
        ),
        patch.object(
            config,
            "_load_tenant_credential",
            new=AsyncMock(return_value=encrypted),
        ),
        patch.object(
            config, "decrypt_credential", side_effect=ValueError("bad secret")
        ),
        patch.object(config, "log_connection_event", new=AsyncMock()),
    ):
        result = await test_connector_connection("connection", None, auth, db)
    assert not result.success and encrypted.last_test_status == "failed"


@pytest.mark.asyncio
async def test_connector_config_create_and_update_without_secrets() -> None:
    import finance_sync.api.v1.connectors_config as config
    from finance_sync.api.v1.connectors_config import (
        ConnectorConfigCreate,
        ConnectorConfigUpdate,
        create_connector_config,
        update_connector_config,
    )

    now = datetime.now(UTC)
    auth = SimpleNamespace(tenant_id="tenant", principal_id="user", user=None)
    settings = SimpleNamespace(is_staging=False)
    db = SimpleNamespace(
        add=MagicMock(), flush=AsyncMock(), scalar=AsyncMock(return_value=None)
    )
    with (
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
        patch.object(config, "log_connection_event", new=AsyncMock()),
    ):
        created = await create_connector_config(
            ConnectorConfigCreate(
                provider_type="manual_expense",
                options={"account_name": "Expenses"},
                description="Expenses",
            ),
            None,
            auth,
            db,
        )
    assert (
        created.provider_type == "manual_expense"
        and created.description == "Expenses"
    )
    credential = SimpleNamespace(
        id="connection",
        provider_key="manual_expense",
        description='{"_label":"Old"}',
        encrypted_payload=None,
        nonce=None,
        status="active",
        selected_accounts=[],
        last_attempt_at=None,
        last_success_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
        last_test_at=None,
        last_test_status=None,
        last_test_error=None,
        credential_status=None,
        last_authenticated_at=None,
        expires_at=None,
        reauth_required_at=None,
        credential_version=1,
    )
    with (
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
        patch.object(
            config,
            "_load_tenant_credential",
            new=AsyncMock(return_value=credential),
        ),
        patch.object(config, "log_connection_event", new=AsyncMock()),
    ):
        updated = await update_connector_config(
            "connection",
            ConnectorConfigUpdate(description="Updated"),
            None,
            auth,
            db,
        )
    assert updated.description == "Updated"


@pytest.mark.asyncio
async def test_connector_connection_test_covers_health_success_and_failure() -> (
    None
):
    import finance_sync.api.v1.connectors_config as config
    from finance_sync.api.v1.connectors_config import test_connector_connection

    now = datetime.now(UTC)

    def credential(provider="bunq"):
        return SimpleNamespace(
            id="connection",
            provider_key=provider,
            description="{}",
            encrypted_payload=None,
            nonce=None,
            status="active",
            selected_accounts=[],
            last_attempt_at=None,
            last_success_at=None,
            last_error=None,
            created_at=now,
            updated_at=now,
            last_test_at=None,
            last_test_status=None,
            last_test_error=None,
            credential_status=None,
            last_authenticated_at=None,
            expires_at=None,
            reauth_required_at=None,
            credential_version=1,
        )

    auth = SimpleNamespace(tenant_id="tenant", principal_id="user", user=None)
    db = SimpleNamespace(flush=AsyncMock())
    connector = SimpleNamespace(
        health=AsyncMock(
            return_value=SimpleNamespace(healthy=True, message="ok")
        ),
        fetch_accounts=AsyncMock(
            return_value=[
                SimpleNamespace(
                    external_account_id="a",
                    name="Checking",
                    provider_metadata={"iban": "NL00"},
                )
            ]
        ),
    )
    registry = SimpleNamespace(get_connector=MagicMock(return_value=connector))
    with (
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=SimpleNamespace()),
        ),
        patch.object(
            config,
            "_load_tenant_credential",
            new=AsyncMock(return_value=credential()),
        ),
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(config, "log_connection_event", new=AsyncMock()),
    ):
        result = await test_connector_connection("connection", None, auth, db)
    assert result.success and result.accounts[0].iban == "NL00"

    connector.health = AsyncMock(
        return_value=SimpleNamespace(healthy=False, message="reauth required")
    )
    with (
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=SimpleNamespace()),
        ),
        patch.object(
            config,
            "_load_tenant_credential",
            new=AsyncMock(return_value=credential()),
        ),
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(config, "log_connection_event", new=AsyncMock()),
    ):
        result = await test_connector_connection("connection", None, auth, db)
    assert not result.success


@pytest.mark.asyncio
async def test_connector_listing_exposes_capabilities_and_market_data() -> None:
    import finance_sync.api.v1.connectors_config as config
    from finance_sync.api.v1.connectors_config import list_available_connectors

    registry = SimpleNamespace(
        list_connectors=lambda: {
            "manual_expense": {
                "display_name": "Manual",
                "sdk_version": "1",
                "spending_capabilities": {},
                "ingestion_methods": ["file"],
            }
        },
        _classes={
            "manual_expense": SimpleNamespace(
                supported_resources={"transactions"}
            )
        },
    )
    settings = SimpleNamespace(is_staging=False)
    with (
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(settings=settings),
        ),
    ):
        result = await list_available_connectors(None)
    manual = next(item for item in result if item.name == "manual_expense")
    assert manual.capabilities == ["transactions"]
    assert any(item.name == "openfigi" for item in result)


@pytest.mark.asyncio
async def test_connector_pause_resume_account_selection_and_inline_file_test() -> (
    None
):
    import finance_sync.api.v1.connectors_config as config
    from finance_sync.api.v1.connectors_config import (
        ConnectorAccountsUpdate,
        InlineTestRequest,
        pause_connector_connection,
        resume_connector_connection,
        set_connection_accounts,
        test_connector_inline,
    )

    now = datetime.now(UTC)
    cred = SimpleNamespace(
        id="connection",
        provider_key="manual_expense",
        description="{}",
        encrypted_payload=None,
        nonce=None,
        status="active",
        selected_accounts=["old"],
        last_attempt_at=None,
        last_success_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
        last_test_at=None,
        last_test_status=None,
        last_test_error=None,
        credential_status=None,
        last_authenticated_at=None,
        expires_at=None,
        reauth_required_at=None,
        credential_version=1,
    )
    schedule = SimpleNamespace(enabled=True, next_run_at=now, updated_at=now)

    class QueryResult:
        def scalar_one_or_none(self):
            return schedule

    db = SimpleNamespace(
        execute=AsyncMock(return_value=QueryResult()),
        flush=AsyncMock(),
        scalars=AsyncMock(),
    )
    auth = SimpleNamespace(tenant_id="tenant", principal_id="user", user=None)
    with (
        patch.object(
            config, "_load_tenant_credential", new=AsyncMock(return_value=cred)
        ),
        patch.object(config, "log_connection_event", new=AsyncMock()),
    ):
        paused = await pause_connector_connection("connection", auth, db)
        cred.status = "paused"
        with patch(
            "finance_sync.services.sync_schedule.compute_next_run",
            return_value=[now],
        ):
            resumed = await resume_connector_connection("connection", auth, db)
        selected = await set_connection_accounts(
            "connection", ConnectorAccountsUpdate(account_ids=["new"]), auth, db
        )
    assert (
        paused.status == "paused"
        and resumed.status == "active"
        and selected.selected_accounts == ["new"]
    )

    class Registry:
        available = ["saxo_investor"]

        def __contains__(self, _item):
            return True

    registry = Registry()
    with (
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(
                settings=SimpleNamespace(is_staging=False)
            ),
        ),
    ):
        inline = await test_connector_inline(
            "saxo_investor", InlineTestRequest(credentials={}, options={}), None
        )
    assert inline.success

    connector = SimpleNamespace(
        health=AsyncMock(
            return_value=SimpleNamespace(healthy=False, message="bad secret")
        ),
        fetch_accounts=AsyncMock(return_value=[]),
    )
    registry.get_connector = MagicMock(return_value=connector)
    with (
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(
                settings=SimpleNamespace(is_staging=False)
            ),
        ),
    ):
        failed = await test_connector_inline(
            "manual_expense",
            InlineTestRequest(credentials={"api_key": "secret"}, options={}),
            None,
        )
    assert not failed.success and "secret" not in failed.message
    connector.health = AsyncMock(
        return_value=SimpleNamespace(healthy=True, message="ok")
    )
    connector.fetch_accounts = AsyncMock(
        return_value=[
            SimpleNamespace(
                external_account_id="a", name="Account", provider_metadata=None
            )
        ]
    )
    with (
        patch.object(config, "_get_registry", return_value=registry),
        patch.object(
            config,
            "get_container",
            return_value=SimpleNamespace(
                settings=SimpleNamespace(is_staging=False)
            ),
        ),
    ):
        succeeded = await test_connector_inline(
            "manual_expense",
            InlineTestRequest(credentials={"api_key": "secret"}, options={}),
            None,
        )
    assert succeeded.success and succeeded.accounts[0].id == "a"


@pytest.mark.asyncio
async def test_mcp_tools_cover_disabled_ai_and_read_service_paths() -> None:
    import finance_sync.mcp.server as server

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    container = SimpleNamespace(
        settings=SimpleNamespace(ai_enabled=False, openbb_api_key=None),
        session_factory=Session,
        fx_service=None,
    )
    with (
        patch.object(server, "_get_tenant_id", return_value="tenant"),
        patch.object(server, "_get_container", return_value=container),
        patch.object(
            server,
            "_get_read_scope",
            new=AsyncMock(
                return_value=SimpleNamespace(account_ids_subquery=lambda: None)
            ),
        ),
    ):
        assert "disabled" in await server.tool_get_summary(None, "7d")
        assert "disabled" in await server.tool_get_daily_briefing(None)

    read_session = SimpleNamespace(aclose=AsyncMock())
    read_service = SimpleNamespace(
        _session=read_session,
        get_cashflow=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"net": "1"})
        ),
    )
    with (
        patch.object(server, "_get_tenant_id", return_value="tenant"),
        patch.object(
            server,
            "_get_read_service",
            new=AsyncMock(return_value=read_service),
        ),
    ):
        assert '"net": "1"' in await server.tool_get_cashflow(None, "7d")
    assert read_session.aclose.await_count == 1

    performance = SimpleNamespace(
        calculate_twr=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"return": 0.1})
        )
    )
    allocation = SimpleNamespace(
        get_allocation=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"items": []})
        )
    )

    class ServiceContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *args):
            return None

    perf_container = SimpleNamespace(
        settings=SimpleNamespace(openbb_api_key=None),
        session_factory=ServiceContext,
        fx_service=None,
    )
    with (
        patch.object(server, "_get_tenant_id", return_value="tenant"),
        patch.object(server, "_get_container", return_value=perf_container),
        patch(
            "finance_sync.services.performance.PerformanceService",
            return_value=performance,
        ),
        patch(
            "finance_sync.services.allocation.AllocationService",
            return_value=allocation,
        ),
    ):
        assert '"return": 0.1' in await server.tool_get_performance(
            None, "portfolio", "1m"
        )
        assert '"items": []' in await server.tool_get_allocation(
            None, "asset_class"
        )


@pytest.mark.asyncio
async def test_mcp_subscription_and_security_tools_serialize_results() -> None:
    from decimal import Decimal

    import finance_sync.mcp.server as server

    subscription = SimpleNamespace(
        id="sub",
        merchant_name="Coffee",
        raw_description="Coffee Shop",
        amount=Decimal("4.50"),
        currency_code="EUR",
        frequency_days=30,
        frequency_label="monthly",
        confidence="high",
        status="active",
        sector=None,
        category="food",
        first_detected_at=None,
        last_detected_at=datetime(2026, 1, 1, tzinfo=UTC),
        occurrence_count=3,
    )
    detector = SimpleNamespace(
        list_subscriptions=AsyncMock(return_value=[subscription])
    )
    scope = SimpleNamespace(account_ids_subquery=lambda: None)
    container = SimpleNamespace(
        session_factory=MagicMock(), settings=SimpleNamespace()
    )
    with (
        patch.object(server, "_get_tenant_id", return_value="tenant"),
        patch.object(server, "_get_container", return_value=container),
        patch.object(
            server, "_get_read_scope", new=AsyncMock(return_value=scope)
        ),
        patch(
            "finance_sync.services.subscription_detector.detector.SubscriptionDetector",
            return_value=detector,
        ),
    ):
        payload = await server.tool_get_subscriptions(None, active_only=True)
    assert "Coffee" in payload and "monthly" in payload


@pytest.mark.asyncio
async def test_mcp_sync_and_intel_listing_tools_cover_filters() -> None:
    import finance_sync.mcp.server as server

    read_session = SimpleNamespace(aclose=AsyncMock())
    read_service = SimpleNamespace(
        _session=read_session,
        list_sync_runs=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: {"runs": []})
        ),
    )
    with (
        patch.object(server, "_get_tenant_id", return_value="tenant"),
        patch.object(
            server,
            "_get_read_service",
            new=AsyncMock(return_value=read_service),
        ),
    ):
        assert '"runs": []' in await server.tool_list_sync_runs(
            None, limit=3, connector="bunq", status="failed"
        )

    session = SimpleNamespace(aclose=AsyncMock())

    class IntelService:
        def __init__(self, _session):
            pass

        async def list_items(self, *args, **kwargs):
            return SimpleNamespace(model_dump=lambda: {"items": []})

        async def list_provider_states(self, *args, **kwargs):
            return [
                SimpleNamespace(
                    provider="openbb", model_dump=lambda: {"provider": "openbb"}
                )
            ]

        async def list_runs(self, *args, **kwargs):
            return [SimpleNamespace(model_dump=lambda: {"status": "ok"})]

    intel_container = SimpleNamespace(session_factory=lambda: session)
    with (
        patch.object(server, "_get_tenant_id", return_value="tenant"),
        patch.object(server, "_get_container", return_value=intel_container),
        patch(
            "finance_sync.services.market_intelligence_read.MarketIntelligenceReadService",
            IntelService,
        ),
    ):
        assert '"items": []' in await server.tool_list_market_intelligence(
            None, provider="openbb"
        )
        assert "openbb" in await server.tool_list_intel_provider_states(
            None, provider="openbb"
        )
        assert '"status": "ok"' in await server.tool_list_intel_runs(
            None, status="ok"
        )
    assert session.aclose.await_count == 3


@pytest.mark.asyncio
async def test_mcp_sync_missing_credentials_and_security_resolution() -> None:
    import finance_sync.mcp.server as server

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _stmt):
            return EmptyResult()

    container = SimpleNamespace(
        session_factory=Session, settings=SimpleNamespace()
    )
    with (
        patch.object(server, "_get_tenant_id", return_value="tenant"),
        patch.object(server, "_get_container", return_value=container),
    ):
        payload = await server.tool_run_sync(None, "bunq")
    assert "No credentials" in payload

    read_session = SimpleNamespace(aclose=AsyncMock())

    class ReadService:
        def __init__(self, session):
            self._session = session

        async def list_securities(self, **kwargs):
            return SimpleNamespace(
                model_dump=lambda: {"items": [{"ticker": "AAPL"}]}
            )

    class ReadSessionContext:
        async def __aenter__(self):
            return read_session

        async def __aexit__(self, *args):
            return None

    with (
        patch.object(
            server,
            "_get_container",
            return_value=SimpleNamespace(session_factory=ReadSessionContext),
        ),
        patch("finance_sync.services.read_api.ReadService", ReadService),
    ):
        payload = await server.tool_resolve_security(None, "AAPL")
    assert "AAPL" in payload
    read_session.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_holding_relevance_tools_cover_filters_and_acknowledgements() -> (
    None
):
    import finance_sync.mcp.server as server

    session = SimpleNamespace(aclose=AsyncMock())
    uow = SimpleNamespace(session=session, commit=AsyncMock())
    service = SimpleNamespace(
        _uow=uow,
        feed=AsyncMock(return_value={"items": []}),
        calendar=AsyncMock(return_value=[{"event": "earnings"}]),
        set_ack=AsyncMock(side_effect=[False, True]),
        correct=AsyncMock(side_effect=[False, True]),
        get_notification_preference=AsyncMock(return_value={"enabled": False}),
        set_notification_preference=AsyncMock(return_value={"enabled": True}),
    )
    with (
        patch.object(
            server, "_get_holding_relevance_service", return_value=service
        ),
        patch.object(
            server, "_auth_principal", return_value=("tenant", "user")
        ),
    ):
        assert '"items": []' in await server.tool_get_holding_feed(
            None, date_from="invalid"
        )
        assert "earnings" in await server.tool_get_holding_calendar(None)
        assert "not_found" in await server.tool_acknowledge_holding_cluster(
            None, "missing"
        )
        assert (
            '"status": "ok"'
            in await server.tool_acknowledge_holding_cluster(None, "cluster")
        )
        assert "not_found" in await server.tool_correct_holding_item(
            None, "missing"
        )
        assert "corrected" in await server.tool_correct_holding_item(
            None, "item", security_id="s"
        )
        assert (
            "enabled"
            in await server.tool_get_holding_notification_preferences(None)
        )
        assert (
            "enabled"
            in await server.tool_set_holding_notification_preferences(
                None, enabled=True
            )
        )
    assert uow.commit.await_count == 2


def test_actual_budget_helpers_classify_accounts_and_categories() -> None:
    from finance_sync.exporter.actual_budget.exporter import (
        ExportResult,
        _category_name,
        is_actual_budget_eligible_account,
    )

    assert is_actual_budget_eligible_account(
        SimpleNamespace(account_type="checking", provider_key="bunq")
    )
    assert not is_actual_budget_eligible_account(
        SimpleNamespace(account_type="securities", provider_key="bunq")
    )
    assert not is_actual_budget_eligible_account(
        SimpleNamespace(account_type="checking", provider_key="trading212")
    )
    assert (
        _category_name(SimpleNamespace(cashflow_suggestion={"value": "Food"}))
        == "Food"
    )
    assert (
        _category_name(
            SimpleNamespace(cashflow_suggestion={"category": "Rent"})
        )
        == "Rent"
    )
    assert (
        _category_name(
            SimpleNamespace(cashflow_suggestion=SimpleNamespace(value="Salary"))
        )
        == "Salary"
    )
    assert _category_name(SimpleNamespace(cashflow_suggestion=None)) is None
    result = ExportResult(
        status="completed", transactions_attempted=2, transactions_exported=2
    )
    assert "completed" in repr(result)


@pytest.mark.asyncio
async def test_actual_budget_account_mapping_and_delivery_cursor_paths() -> (
    None
):
    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
    from finance_sync.exporter.actual_budget.exporter import (
        ActualBudgetExporter,
    )
    from finance_sync.exporter.actual_budget.client import (
        ActualBudgetConnectionError,
    )

    class Result:
        def __init__(self, value=None, rows=()):
            self.value, self.rows = value, list(rows)

        def scalar_one_or_none(self):
            return self.value

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class Session:
        def __init__(self, results):
            self.results = iter(results)
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _stmt):
            return next(self.results)

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    exporter = ActualBudgetExporter(MagicMock(), ActualBudgetConfig(), "tenant")
    account = SimpleNamespace(id="a", name="Checking")
    client = SimpleNamespace(
        get_account_by_name=AsyncMock(return_value=None),
        get_or_create_account=AsyncMock(
            return_value={"id": "ab", "name": "Checking"}
        ),
    )
    session = Session([Result(None)])
    assert (await exporter._resolve_ab_account(session, account, client))[
        "id"
    ] == "ab"
    mapping = SimpleNamespace(
        ab_account_name="Checking",
        ab_account_id="old",
    )
    client.get_account_by_name = AsyncMock(return_value=None)
    session = Session([Result(mapping)])
    assert (await exporter._resolve_ab_account(session, account, client))[
        "id"
    ] == "ab"
    client.get_account_by_name = AsyncMock(return_value={"id": "existing"})
    session = Session([Result(mapping)])
    assert await exporter._resolve_ab_account(session, account, client) == {
        "id": "existing"
    }
    session = Session([Result(None)])
    await exporter._update_export_delivery(
        session, account_id="a", transaction_ids=[]
    )
    await exporter._update_export_delivery(
        session, account_id="a", transaction_ids=["tx"]
    )
    delivery = SimpleNamespace()
    session = Session([Result(delivery)])
    await exporter._update_export_delivery(
        session, account_id="a", transaction_ids=["tx2"]
    )
    assert delivery.last_exported_transaction_id == "tx2"


@pytest.mark.asyncio
async def test_actual_budget_csv_and_completion_paths(tmp_path) -> None:
    import finance_sync.exporter.actual_budget.exporter as module
    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
    from finance_sync.exporter.actual_budget.exporter import (
        ActualBudgetExporter,
    )

    exporter = ActualBudgetExporter(MagicMock(), ActualBudgetConfig(), "tenant")
    session = SimpleNamespace()
    exporter._fetch_pending_transactions_for_csv = AsyncMock(return_value=[])
    assert (
        await exporter._write_csv(
            session,
            account_ids=["a"],
            since=datetime.now(UTC),
            output_dir=str(tmp_path),
        )
        is None
    )
    exporter._fetch_pending_transactions_for_csv = AsyncMock(
        return_value=[SimpleNamespace(id="t")]
    )
    with patch.object(
        module,
        "map_transaction_to_csv_row",
        return_value={
            "Date": "2026-01-01",
            "Payee": "Coffee",
            "Category": "Food",
            "Notes": "",
            "Amount": "1",
        },
    ):
        path = await exporter._write_csv(
            session,
            account_ids=["a"],
            since=datetime.now(UTC),
            output_dir=str(tmp_path),
        )
    assert path is not None
    await exporter._complete_run(None, status="completed")


@pytest.mark.asyncio
async def test_actual_budget_query_helpers_use_delivery_cursor_and_filter_accounts() -> (
    None
):
    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
    from finance_sync.exporter.actual_budget.exporter import (
        ActualBudgetExporter,
    )

    class Result:
        def __init__(self, rows=(), value=None):
            self.rows, self.value = list(rows), value

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self, results):
            self.results = iter(results)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _stmt):
            return next(self.results)

    account = SimpleNamespace(
        account_type="checking", provider_key="bunq", id="a"
    )
    broker = SimpleNamespace(
        account_type="checking", provider_key="trading212", id="b"
    )
    exporter = ActualBudgetExporter(
        lambda: Session([Result([account, broker])]),
        ActualBudgetConfig(),
        "tenant",
    )
    assert await exporter._load_accounts(None) == [account]
    delivery = SimpleNamespace(
        last_exported_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    tx = SimpleNamespace(id="tx")
    session = Session([Result(value=delivery), Result([tx])])
    assert await exporter._fetch_pending_transactions(
        session, account_id="a", since=datetime(2026, 1, 1, tzinfo=UTC)
    ) == [tx]


@pytest.mark.asyncio
async def test_actual_budget_export_runs_normal_and_transfer_transactions() -> (
    None
):
    import finance_sync.exporter.actual_budget.exporter as module
    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
    from finance_sync.exporter.actual_budget.client import (
        ActualBudgetConnectionError,
    )
    from finance_sync.exporter.actual_budget.exporter import (
        ActualBudgetExporter,
    )
    from finance_sync.models.enums import SyncRunStatus

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class Client:
        def __init__(self, _config):
            self.transfer_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def transfer_exists(self, reference):
            self.transfer_count += 1
            return self.transfer_count == 2

        async def get_or_create_account(self, name, off_budget=False):
            return {"name": name}

        async def create_transfer(self, **kwargs):
            return None

        async def import_transactions_batch(self, **kwargs):
            return len(kwargs["transactions"])

    config = ActualBudgetConfig(
        transfer_account_name_overrides={"other": "Other"}
    )
    exporter = ActualBudgetExporter(lambda: Session(), config, "tenant")
    account = SimpleNamespace(id="a", name="Checking")
    txs = [
        SimpleNamespace(
            id="normal",
            transaction_type="payment",
            counterparty_account_reference=None,
        ),
        SimpleNamespace(
            id="transfer1",
            transaction_type="transfer",
            counterparty_account_reference="other",
            amount=-10,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        SimpleNamespace(
            id="transfer2",
            transaction_type="transfer",
            counterparty_account_reference="other",
            amount=10,
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        ),
    ]
    with (
        patch.object(module, "ActualBudgetClient", Client),
        patch.object(
            exporter, "_load_accounts", new=AsyncMock(return_value=[account])
        ),
        patch.object(
            exporter,
            "_resolve_ab_account",
            new=AsyncMock(return_value={"name": "Checking"}),
        ),
        patch.object(
            exporter,
            "_fetch_pending_transactions",
            new=AsyncMock(return_value=txs),
        ),
        patch.object(exporter, "_update_export_delivery", new=AsyncMock()),
        patch.object(exporter, "_write_csv", new=AsyncMock(return_value=None)),
        patch.object(module, "map_transaction", return_value={"amount": 1}),
    ):
        result = await exporter.run_export(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert result.status == "completed" and result.transactions_exported == 2
    with (
        patch.object(module, "ActualBudgetClient", Client),
        patch.object(
            exporter, "_load_accounts", new=AsyncMock(return_value=[account])
        ),
        patch.object(
            exporter, "_resolve_ab_account", new=AsyncMock(return_value=None)
        ),
        patch.object(exporter, "_write_csv", new=AsyncMock(return_value=None)),
    ):
        skipped = await exporter.run_export(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert skipped.status == "completed" and skipped.accounts_mapped == 0

    class FailingClient(Client):
        async def __aenter__(self):
            raise ActualBudgetConnectionError("actual unavailable")

    with patch.object(module, "ActualBudgetClient", FailingClient):
        failed = await exporter.run_export(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert failed.status == "failed" and failed.error_message


@pytest.mark.asyncio
async def test_actual_budget_exporter_completes_empty_projection() -> None:
    import finance_sync.exporter.actual_budget.exporter as module
    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
    from finance_sync.exporter.actual_budget.exporter import (
        ActualBudgetExporter,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class Client:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    exporter = ActualBudgetExporter(
        lambda: Session(), ActualBudgetConfig(), "tenant"
    )
    with (
        patch.object(module, "ActualBudgetClient", Client),
        patch.object(
            exporter, "_load_accounts", new=AsyncMock(return_value=[])
        ),
        patch.object(exporter, "_write_csv", new=AsyncMock(return_value=None)),
    ):
        result = await exporter.run_export(since=datetime.now(UTC))
    assert result.status == "completed" and result.transactions_exported == 0


def test_degiro_pension_parsing_helpers_cover_locale_and_corporate_actions() -> (
    None
):
    from decimal import Decimal
    from finance_sync.connectors.degiro_pension import (
        _Row,
        _clean,
        _corporate_action_ratio,
        _currency,
        _decimal,
        _hash,
        _key,
        _parse_datetime,
    )

    assert _key("Waarde in EUR!") == "waardeineur"
    assert _clean(datetime(2026, 1, 2, 3, 4)) == "2026-01-02 03:04:00"
    assert _clean(date(2026, 1, 2)) == "2026-01-02"
    assert _decimal("€ 1.234,56") == Decimal("1234.56")
    assert _decimal("(12,5)") == Decimal("-12.5")
    assert _decimal("--") is None
    with pytest.raises(ValueError):
        _decimal("x", required=True)
    assert _currency("usd") == "USD"
    assert _currency("EU") == "EUR"
    assert _corporate_action_ratio("Corporate action 3:2") == Decimal("1.5")
    assert _corporate_action_ratio("ordinary dividend") is None
    assert _parse_datetime("02-01-2026", "12:30").tzinfo is not None
    with pytest.raises(ValueError):
        _parse_datetime("not-a-date")
    assert _hash("A", "B") == _hash("a", "b")
    row = _Row(["Currency", "Currency", "Amount"], ["EUR", "USD", "10"])
    assert row.get("currency") == "EUR"
    assert row.get("currency", occurrence=1) == "USD"
    assert row.get("missing") == ""


def test_health_monitor_helpers_cover_state_checks_and_thresholds(
    tmp_path,
) -> None:
    import finance_sync.monitoring.health_monitor as monitor

    state_path = tmp_path / "state.json"
    with patch.object(monitor, "get_state_file", return_value=str(state_path)):
        assert monitor.load_state()["checks"] == []
        monitor.save_state({"checks": ["ok"]})
        assert monitor.load_state()["checks"] == ["ok"]
        state_path.write_text("not-json")
        assert monitor.load_state()["last_status"] is None
    assert monitor.check_resource_thresholds(
        {"app": {"cpu_percent": 95, "mem_percent": 95}, "_error": "x"}
    )
    assert (
        monitor.build_crash_marker(datetime(2026, 1, 2, tzinfo=UTC))
        == "<!-- crash-monitor:2026-01-02 -->"
    )
    assert (
        monitor.build_resource_marker(datetime(2026, 1, 2, tzinfo=UTC))
        == "<!-- resource-monitor:2026-01-02 -->"
    )
    with patch.object(
        monitor.subprocess, "run", return_value=SimpleNamespace(stdout="200")
    ):
        assert monitor.check_health("https://example.test") == 200
    with patch.object(
        monitor.subprocess, "run", side_effect=RuntimeError("offline")
    ):
        assert monitor.check_health("https://example.test") == 999


@pytest.mark.asyncio
async def test_wealthfolio_exporter_query_and_delivery_paths() -> None:
    from uuid import uuid4
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    class ScalarRows:
        def __init__(self, rows=(), scalar=None):
            self.rows, self.scalar_value = list(rows), scalar

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def scalar_one_or_none(self):
            return self.scalar_value

    class Session:
        def __init__(self, result):
            self.result = result
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _stmt):
            return self.result

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def commit(self):
            return None

    account = SimpleNamespace(id="a", name="Broker", security_id=None)
    security = SimpleNamespace(id="s")
    observation = SimpleNamespace(security_id="s")
    holding_one = SimpleNamespace(
        security_id="s", observed_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    holding_two = SimpleNamespace(
        security_id="s", observed_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    tx = SimpleNamespace(
        id=uuid4(),
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        external_transaction_id="ext",
    )

    def factory(result):
        session = Session(result)
        return session

    exporter = WealthfolioExporter(
        lambda: factory(ScalarRows([account])),
        WealthfolioConfig(),
        tenant_id="tenant",
    )
    with (
        patch(
            "finance_sync.services.account_selection.load_account_selection",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "finance_sync.services.account_selection.account_is_selected",
            return_value=True,
        ),
    ):
        assert await exporter._load_accounts(None) == [account]

    exporter._session_factory = lambda: factory(ScalarRows([security]))
    assert await exporter._load_securities() == {"s": security}
    exporter._session_factory = lambda: factory(ScalarRows([observation]))
    assert (await exporter._load_security_metadata({"s"}))["s"] == [observation]
    exporter._session_factory = lambda: factory(
        ScalarRows([holding_one, holding_two])
    )
    assert await exporter._fetch_current_holdings(account_id="a") == [
        holding_one
    ]
    assert await exporter._fetch_historical_holdings(account_id="a") == [
        holding_one,
        holding_two,
    ]
    assert await exporter._fetch_tax_lots(account_id="a") == [
        holding_one,
        holding_two,
    ]
    exporter._session_factory = lambda: factory(ScalarRows([tx]))
    assert await exporter._fetch_pending_transactions(
        account_id="a", since=datetime(2026, 1, 1, tzinfo=UTC)
    ) == [tx]
    exporter._session_factory = lambda: factory(ScalarRows(["ext"]))
    assert await exporter._transaction_external_ids("a") == {"ext"}
    exporter._session_factory = lambda: factory(
        ScalarRows(scalar=datetime(2026, 1, 1, tzinfo=UTC))
    )
    assert await exporter._earliest_transaction_time("a") == datetime(
        2026, 1, 1, tzinfo=UTC
    )
    exporter._session_factory = lambda: factory(
        ScalarRows([holding_one.observed_at, holding_two.observed_at])
    )
    assert await exporter._has_historical_holdings("a")
    exporter._session_factory = lambda: factory(ScalarRows(scalar=None))
    assert await exporter._last_export_time() < datetime.now(UTC)
    assert await exporter._delivery_cursor(account_id="a") is None
    await exporter._update_wealthfolio_delivery(account_id="a", transactions=[])


@pytest.mark.asyncio
async def test_wealthfolio_quote_history_filters_manual_assets_and_quarantines_errors() -> (
    None
):
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    exporter = WealthfolioExporter(
        MagicMock(), WealthfolioConfig(), tenant_id="tenant"
    )
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    prices = [
        SimpleNamespace(
            security_id="s1",
            timestamp=stamp,
            price_close="10",
            price_open=None,
            price_high=None,
            price_low=None,
            volume=None,
            currency_code="EUR",
        ),
        SimpleNamespace(
            security_id="s2",
            timestamp=stamp,
            price_close="20",
            price_open="19",
            price_high="21",
            price_low="18",
            volume="5",
            currency_code="EUR",
        ),
        SimpleNamespace(
            security_id="missing",
            timestamp=stamp,
            price_close="30",
            price_open=None,
            price_high=None,
            price_low=None,
            volume=None,
            currency_code="EUR",
        ),
    ]
    exporter._load_security_prices = AsyncMock(return_value=prices)
    exporter._wealthfolio_quote_quarantine = AsyncMock(
        return_value={"quarantined"}
    )
    client = SimpleNamespace(
        get_assets=AsyncMock(
            return_value=[
                {"id": "asset-1", "symbol": "S1", "quoteMode": "MARKET"},
                {"id": "asset-2", "symbol": "S2", "quoteMode": "MANUAL"},
            ]
        ),
        upsert_quote=AsyncMock(),
    )
    with (
        patch.object(
            exporter, "_clear_wealthfolio_quote_failure", new=AsyncMock()
        ),
        patch.object(
            exporter, "_record_wealthfolio_quote_failure", new=AsyncMock()
        ),
    ):
        count = await exporter._sync_quote_history(
            wf_client=client,
            security_map={
                "s1": SimpleNamespace(ticker="S1", isin=None),
                "s2": SimpleNamespace(ticker="S2", isin=None),
            },
        )
    assert count == 1
    client.upsert_quote.assert_awaited_once()

    exporter._load_security_prices = AsyncMock(return_value=[prices[0]])
    client.upsert_quote = AsyncMock(side_effect=RuntimeError("rejected"))
    record_failure = AsyncMock()
    with patch.object(
        exporter, "_record_wealthfolio_quote_failure", new=record_failure
    ):
        assert (
            await exporter._sync_quote_history(
                wf_client=client,
                security_map={"s1": SimpleNamespace(ticker="S1", isin=None)},
            )
            == 0
        )
    record_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_wealthfolio_fx_history_creates_pairs_and_rejects_invalid_rates() -> (
    None
):
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    exporter = WealthfolioExporter(
        MagicMock(), WealthfolioConfig(), tenant_id="tenant"
    )
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    exporter._load_fx_rates = AsyncMock(
        return_value=[
            SimpleNamespace(
                base_currency="USD",
                quote_currency="EUR",
                rate="0.9",
                timestamp=stamp,
            ),
            SimpleNamespace(
                base_currency="GBP",
                quote_currency="EUR",
                rate="1.1",
                timestamp=stamp,
            ),
        ]
    )
    client = SimpleNamespace(
        get_assets=AsyncMock(
            return_value=[
                {"id": "usd-eur", "instrumentSymbol": "USD", "quoteCcy": "EUR"}
            ]
        ),
        add_exchange_rate=AsyncMock(return_value={"id": "gbp-eur"}),
        upsert_quote=AsyncMock(),
    )
    assert await exporter._sync_fx_history(client) == 2
    client.add_exchange_rate.assert_awaited_once_with(
        from_currency="GBP", to_currency="EUR", rate="1.1"
    )
    assert client.upsert_quote.await_count == 2

    exporter._load_fx_rates = AsyncMock(
        return_value=[
            SimpleNamespace(
                base_currency="USD",
                quote_currency="EUR",
                rate="0",
                timestamp=stamp,
            )
        ]
    )
    with pytest.raises(ValueError, match="Ongeldige FX-observatie"):
        await exporter._sync_fx_history(client)


@pytest.mark.asyncio
async def test_wealthfolio_historical_holdings_projects_snapshots_and_rejections() -> (
    None
):
    import finance_sync.exporter.wealthfolio.exporter as module
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    exporter = WealthfolioExporter(
        MagicMock(), WealthfolioConfig(), tenant_id="tenant"
    )
    exporter._fetch_historical_holdings = AsyncMock(
        return_value=[
            SimpleNamespace(
                id="h1",
                security_id="s",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                quantity="2",
                market_value="20",
                price=None,
            ),
            SimpleNamespace(
                id="h2",
                security_id="s",
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                quantity="1",
                market_value=None,
                price="11",
            ),
            SimpleNamespace(
                id="h3",
                security_id="s",
                observed_at=datetime(2026, 1, 3, tzinfo=UTC),
                quantity="0",
                market_value=None,
                price="11",
            ),
        ]
    )
    exporter._load_open_cost_basis = AsyncMock(return_value={"s": ("9", 2)})
    valid = SimpleNamespace(
        blocking_findings=[],
        exportable_holdings=exporter._fetch_historical_holdings.return_value,
    )
    client = SimpleNamespace(
        import_holdings=AsyncMock(return_value={"validationErrors": []})
    )
    with (
        patch.object(module, "validate_holdings", return_value=valid),
        patch.object(
            module,
            "map_holding_to_wf_row",
            return_value={
                "date": "2026-01-01",
                "symbol": "S",
                "isin": "",
                "quantity": "2",
                "avgCost": "",
                "currency": "EUR",
            },
        ),
    ):
        assert (
            await exporter._sync_historical_holdings(
                wf_client=client,
                fs_account=SimpleNamespace(id="a", name="Broker"),
                wf_account_id="wf",
                security_map={"s": SimpleNamespace(ticker="S")},
            )
            == []
        )
    client.import_holdings.assert_awaited_once()

    client.import_holdings = AsyncMock(
        return_value={"validationErrors": ["bad"]}
    )
    with (
        patch.object(module, "validate_holdings", return_value=valid),
        patch.object(
            module,
            "map_holding_to_wf_row",
            return_value={
                "date": "2026-01-01",
                "symbol": "S",
                "isin": "",
                "quantity": "2",
                "avgCost": "9",
                "currency": "EUR",
            },
        ),
    ):
        rejected = await exporter._sync_historical_holdings(
            wf_client=client,
            fs_account=SimpleNamespace(id="a", name="Broker"),
            wf_account_id="wf",
            security_map={"s": SimpleNamespace(ticker="S")},
        )
    assert rejected[0]["error"]
    exporter._wf_config.export_holdings = False
    assert (
        await exporter._sync_historical_holdings(
            wf_client=client,
            fs_account=SimpleNamespace(id="a", name="Broker"),
            wf_account_id="wf",
            security_map={},
        )
        == []
    )


@pytest.mark.asyncio
async def test_wealthfolio_catalog_and_historical_export_empty_and_nonempty_paths(
    tmp_path,
) -> None:
    import finance_sync.exporter.wealthfolio.exporter as module
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    exporter = WealthfolioExporter(
        MagicMock(), WealthfolioConfig(), tenant_id="tenant"
    )
    exporter._load_securities = AsyncMock(return_value={})
    assert await exporter.export_asset_catalog(output_dir=tmp_path) is None
    exporter._load_securities = AsyncMock(
        return_value={"s": SimpleNamespace(id="s")}
    )
    exporter._load_security_metadata = AsyncMock(return_value={})
    with (
        patch.object(module, "map_security_catalog_to_csv", return_value="csv"),
        patch.object(
            exporter, "_write_csv_file", return_value=tmp_path / "catalog.csv"
        ),
    ):
        assert (
            await exporter.export_asset_catalog(output_dir=tmp_path)
            == tmp_path / "catalog.csv"
        )
    exporter._load_accounts = AsyncMock(return_value=[])
    assert await exporter.export_historical_holdings(output_dir=tmp_path) == []
    exporter._load_accounts = AsyncMock(
        return_value=[SimpleNamespace(id="a", name="A")]
    )
    exporter._load_securities = AsyncMock(return_value={})
    exporter._fetch_historical_holdings = AsyncMock(return_value=[])
    assert await exporter.export_historical_holdings(output_dir=tmp_path) == []


@pytest.mark.asyncio
async def test_wealthfolio_push_completes_empty_projection() -> None:
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
                scalar_one_or_none=lambda: None,
            )

        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

    exporter = WealthfolioExporter(
        lambda: Session(), WealthfolioConfig(), tenant_id="tenant"
    )
    client = SimpleNamespace(
        delete_accounts_not_owned_by_finance_sync=AsyncMock(return_value=0)
    )
    now = datetime.now(UTC)
    with (
        patch.object(
            exporter, "_load_securities", new=AsyncMock(return_value={})
        ),
        patch.object(
            exporter,
            "_build_preflight_manifest",
            new=AsyncMock(return_value={}),
        ),
        patch.object(
            exporter, "_load_accounts", new=AsyncMock(return_value=[])
        ),
        patch.object(exporter, "_store_preflight_manifest", new=AsyncMock()),
        patch.object(
            exporter, "_sync_quote_history", new=AsyncMock(return_value=0)
        ),
        patch.object(
            exporter, "_sync_fx_history", new=AsyncMock(return_value=0)
        ),
        patch.object(exporter, "_complete_run", new=AsyncMock()),
    ):
        result = await exporter.push_to_wealthfolio(
            client, accounts=[], since=now
        )
    assert result["imported"] == 0 and result["errors"] == []


@pytest.mark.asyncio
async def test_wealthfolio_push_maps_and_advances_delivery_cursor() -> None:
    from decimal import Decimal

    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
                scalar_one_or_none=lambda: None,
            )

        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

    account = SimpleNamespace(
        id="account",
        name="Checking",
        account_type="checking",
        currency_code="EUR",
        provider_key="bunq",
        provider_metadata={},
    )
    transaction = SimpleNamespace(
        id="transaction",
        external_transaction_id="external",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        transaction_type="payment",
        amount=Decimal("-10"),
        currency_code="EUR",
        amount_in_base=None,
        base_currency_code=None,
        fx_rate=None,
        quantity=None,
        unit_price=None,
        fee_amount=None,
        fee_currency_code=None,
        description="Coffee",
        account_id="account",
        security_id=None,
        provider_key="bunq",
        status="booked",
        booked_at=datetime(2026, 1, 1, tzinfo=UTC),
        revision=1,
    )
    exporter = WealthfolioExporter(
        lambda: Session(), WealthfolioConfig(), tenant_id="tenant"
    )
    client = SimpleNamespace(
        delete_accounts_not_owned_by_finance_sync=AsyncMock(return_value=0),
        delete_activities_not_in=AsyncMock(),
        push_activities=AsyncMock(
            return_value={"imported": 1, "skipped": 0, "failed": 0}
        ),
    )
    with (
        patch.object(
            exporter, "_load_securities", new=AsyncMock(return_value={})
        ),
        patch.object(
            exporter,
            "_build_preflight_manifest",
            new=AsyncMock(return_value={}),
        ),
        patch.object(exporter, "_store_preflight_manifest", new=AsyncMock()),
        patch.object(
            exporter,
            "_ensure_wf_account",
            new=AsyncMock(return_value={"id": "wf-account"}),
        ),
        patch.object(
            exporter, "_delivery_cursor", new=AsyncMock(return_value=None)
        ),
        patch.object(
            exporter,
            "_transaction_external_ids",
            new=AsyncMock(return_value={"external"}),
        ),
        patch.object(
            exporter,
            "_fetch_pending_transactions",
            new=AsyncMock(return_value=[transaction]),
        ),
        patch.object(
            exporter,
            "_sync_and_reconcile_holdings",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(exporter, "_update_wealthfolio_delivery", new=AsyncMock()),
        patch.object(
            exporter, "_sync_quote_history", new=AsyncMock(return_value=0)
        ),
        patch.object(
            exporter, "_sync_fx_history", new=AsyncMock(return_value=0)
        ),
        patch.object(
            exporter,
            "_reconcile_activity_totals",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(exporter, "_complete_run", new=AsyncMock()),
    ):
        result = await exporter.push_to_wealthfolio(
            client, accounts=[account], since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert result["imported"] == 1 and result["errors"] == []
    client.push_activities.assert_awaited_once()


@pytest.mark.asyncio
async def test_wealthfolio_push_rebuild_handles_partial_remote_rejection() -> (
    None
):
    from decimal import Decimal
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
                scalar_one_or_none=lambda: None,
            )

        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

    account = SimpleNamespace(
        id="account",
        name="Checking",
        account_type="checking",
        currency_code="EUR",
        provider_key="bunq",
        provider_metadata={},
    )
    transaction = SimpleNamespace(
        id="transaction",
        external_transaction_id="external",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        transaction_type="payment",
        amount=Decimal("-10"),
        currency_code="EUR",
        amount_in_base=None,
        base_currency_code=None,
        fx_rate=None,
        quantity=None,
        unit_price=None,
        fee_amount=None,
        fee_currency_code=None,
        description="Coffee",
        account_id="account",
        security_id=None,
        provider_key="bunq",
        status="booked",
        booked_at=datetime(2026, 1, 1, tzinfo=UTC),
        revision=1,
    )
    exporter = WealthfolioExporter(
        lambda: Session(), WealthfolioConfig(), tenant_id="tenant"
    )
    client = SimpleNamespace(
        delete_accounts_not_owned_by_finance_sync=AsyncMock(return_value=1),
        delete_activities=AsyncMock(),
        push_activities=AsyncMock(
            return_value={"imported": 0, "skipped": 0, "failed": 1}
        ),
        get_performance_history=AsyncMock(return_value={"series": []}),
    )
    with (
        patch.object(
            exporter, "_load_securities", new=AsyncMock(return_value={})
        ),
        patch.object(
            exporter,
            "_build_preflight_manifest",
            new=AsyncMock(return_value={}),
        ),
        patch.object(exporter, "_store_preflight_manifest", new=AsyncMock()),
        patch.object(
            exporter,
            "_ensure_wf_account",
            new=AsyncMock(return_value={"id": "wf-account"}),
        ),
        patch.object(
            exporter, "_delivery_cursor", new=AsyncMock(return_value=None)
        ),
        patch.object(
            exporter,
            "_has_historical_holdings",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            exporter,
            "_sync_historical_holdings",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            exporter,
            "_fetch_pending_transactions",
            new=AsyncMock(return_value=[transaction]),
        ),
        patch.object(
            exporter,
            "_earliest_transaction_time",
            new=AsyncMock(return_value=transaction.occurred_at),
        ),
        patch.object(
            exporter,
            "_sync_and_reconcile_holdings",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            exporter, "_sync_quote_history", new=AsyncMock(return_value=0)
        ),
        patch.object(
            exporter, "_sync_fx_history", new=AsyncMock(return_value=0)
        ),
        patch.object(exporter, "_complete_run", new=AsyncMock()),
    ):
        result = await exporter.push_to_wealthfolio(
            client,
            accounts=[account],
            since=transaction.occurred_at,
            full_sync=True,
            rebuild=True,
        )
    assert result["failed"] == 1 and result["errors"]
    client.delete_activities.assert_awaited_once_with("wf-account")


def test_wealthfolio_exporter_writes_sanitized_files_and_manifest(
    tmp_path,
) -> None:
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    exporter = WealthfolioExporter(
        MagicMock(), WealthfolioConfig(), tenant_id="tenant"
    )
    csv_path = exporter._write_csv_file(
        content="id,amount\n1,2\n",
        export_dir=tmp_path,
        prefix="Transactions / Broker",
    )
    assert csv_path.read_text() == "id,amount\n1,2\n"
    assert "Transactions___Broker" in csv_path.name
    manifest = exporter._write_manifest(
        [str(csv_path)], tmp_path, attempted=1, exported=1, holdings=0
    )
    assert '"transactions_exported": 1' in manifest.read_text()


@pytest.mark.asyncio
async def test_cli_wealthfolio_guard_paths() -> None:
    from argparse import Namespace
    import finance_sync.cli as cli

    disabled_settings = SimpleNamespace(
        is_production=False,
        log_level="INFO",
        exporter_wealthfolio_enabled=False,
    )
    with (
        patch.object(cli, "Settings", return_value=disabled_settings),
        patch.object(cli, "configure_logging"),
    ):
        with pytest.raises(SystemExit):
            await cli._cmd_wealthfolio(
                Namespace(wf_command="export", tenant_id="tenant")
            )

    container = SimpleNamespace(
        settings=SimpleNamespace(
            wealthfolio_server_url="",
            wealthfolio_password="",
            wealthfolio_request_timeout=1,
        ),
        session_factory=MagicMock(),
    )
    args = Namespace(
        server_url=None,
        password=None,
        days_back=30,
        full_history=False,
        rebuild=False,
        dry_run=False,
        account_ids=None,
    )
    with pytest.raises(SystemExit):
        await cli._cmd_wealthfolio_push(args, container, "tenant")
    smoke_args = Namespace(
        server_url=None,
        password=None,
        allow_prod=False,
        account_ids=None,
        days_back=30,
    )
    with pytest.raises(SystemExit):
        await cli._cmd_wealthfolio_smoke(smoke_args, container, "tenant")


def test_connector_config_schema_and_safe_response_helpers() -> None:
    import finance_sync.api.v1.connectors_config as config

    assert config._account_enumeration_error_is_fatal("bunq")
    assert not config._account_enumeration_error_is_fatal("trading212")
    creds, options = config._get_connector_credential_schema("trading212")
    assert {field["key"] for field in creds} == {"api_key", "api_secret"}
    assert any(field["key"] == "demo" for field in options)
    assert config._get_connector_credential_schema("unknown") == ([], [])
    staging_creds, staging_options = config._staging_connector_schema("bunq")
    assert staging_creds[0]["required"] is False
    assert staging_options[0]["key"] == "data_source"
    assert (
        config._credential_secrets(
            SimpleNamespace(encrypted_payload=None), None
        )
        == []
    )
    row = SimpleNamespace(
        id="id",
        provider_key="manual_expense",
        description='{"_label":"Expenses", "region":"eu"}',
        encrypted_payload=None,
        nonce=None,
        status="active",
        selected_accounts=[],
        last_attempt_at=None,
        last_success_at=None,
        last_error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    response = config._credential_response(row)
    assert response.description == "Expenses"
    assert response.options == {"region": "eu"}
    assert response.is_configured


@pytest.mark.asyncio
async def test_cli_exporter_disabled_guards() -> None:
    from argparse import Namespace
    import finance_sync.cli as cli

    cases = [
        ("_cmd_ghostfolio", "exporter_ghostfolio_enabled", "ghostfolio"),
        ("_cmd_investbrain", "exporter_investbrain_enabled", "investbrain"),
        ("_cmd_securo", "exporter_securo_enabled", "securo"),
        (
            "_cmd_actual_budget",
            "exporter_actual_budget_enabled",
            "actual-budget",
        ),
    ]
    for function_name, flag, _name in cases:
        settings = SimpleNamespace(
            **{flag: False, "is_production": False, "log_level": "INFO"}
        )
        with (
            patch.object(cli, "Settings", return_value=settings),
            patch.object(cli, "configure_logging"),
        ):
            with pytest.raises(SystemExit):
                await getattr(cli, function_name)(Namespace())


@pytest.mark.asyncio
async def test_cli_ghostfolio_and_investbrain_missing_token_guards() -> None:
    from argparse import Namespace
    import finance_sync.cli as cli

    class Dispose:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return None

    class Tenants:
        async def list(self, limit=1):
            return [SimpleNamespace(id="tenant")]

    for function_name, enabled, token_attr in [
        (
            "_cmd_ghostfolio",
            "exporter_ghostfolio_enabled",
            "ghostfolio_access_token",
        ),
        (
            "_cmd_investbrain",
            "exporter_investbrain_enabled",
            "investbrain_access_token",
        ),
    ]:
        settings = SimpleNamespace(**{enabled: True, token_attr: ""})
        container = SimpleNamespace(
            settings=settings,
            dispose=lambda: Dispose(),
            session_factory=lambda: SessionContext(),
        )
        with (
            patch.object(cli, "Settings", return_value=settings),
            patch.object(
                cli.Container, "from_settings", return_value=container
            ),
            patch.object(
                cli,
                "UnitOfWork",
                return_value=SimpleNamespace(tenants=Tenants()),
            ),
        ):
            with pytest.raises(SystemExit):
                await getattr(cli, function_name)(Namespace(access_token=None))


@pytest.mark.asyncio
async def test_cli_wealthfolio_dry_run_path() -> None:
    from argparse import Namespace
    import finance_sync.cli as cli
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig

    settings = SimpleNamespace(
        wealthfolio_server_url="https://wf.test",
        wealthfolio_request_timeout=5,
        wealthfolio_password="",
    )
    container = SimpleNamespace(settings=settings, session_factory=MagicMock())
    fake_exporter = SimpleNamespace(_load_accounts=AsyncMock(return_value=[]))
    args = Namespace(
        server_url="https://wf.test",
        password="secret",
        days_back=30,
        full_history=False,
        rebuild=False,
        dry_run=True,
        account_ids=None,
    )
    with (
        patch(
            "finance_sync.exporter.wealthfolio.config.WealthfolioConfig.from_settings",
            return_value=WealthfolioConfig(),
        ),
        patch(
            "finance_sync.exporter.wealthfolio.exporter.WealthfolioExporter",
            return_value=fake_exporter,
        ),
    ):
        await cli._cmd_wealthfolio_push(args, container, "tenant")
    fake_exporter._load_accounts.assert_awaited_once_with(None)
