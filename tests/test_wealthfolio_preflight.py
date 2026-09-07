from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from finance_sync.services.wealthfolio_preflight import (
    WealthfolioDestinationProbe,
    missing_wealthfolio_assets,
    probe_wealthfolio_destination,
    quantity_event_ratio,
    validate_activity_semantics,
    validate_holdings,
    validate_transaction_stream,
    validate_transfer_rows,
)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"split_ratio": "2"}, "2"),
        ({"split_ratio": "2:1"}, "2"),
        ({"fields": {"splitRatio": "1/10"}}, "0.1"),
        ({"fields": {"quantityMultiplier": "1.5"}}, "1.5"),
        (
            {"fields": {"split_numerator": 3, "split_denominator": 2}},
            "1.5",
        ),
        (
            {"fields": {"event": {"newQuantity": 3, "oldQuantity": 2}}},
            "1.5",
        ),
        (
            {"fields": {"new_units": 3, "old_units": 2}},
            "1.5",
        ),
    ],
)
def test_quantity_event_ratio_reads_canonical_provider_metadata_contract(
    metadata, expected
) -> None:
    assert str(quantity_event_ratio(metadata)) == expected


def _holding(**changes):
    values = {
        "id": "h-1",
        "quantity": 10,
        "market_value": 100,
        "price": 10,
        "cost_basis": 90,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_incomplete_valuation_is_quarantined_and_cost_basis_is_warning():
    result = validate_holdings(
        [
            _holding(id="bad", market_value=None, price=None),
            _holding(id="cost", cost_basis=None),
        ]
    )

    assert [item.id for item in result.quarantined_holdings] == ["bad"]
    assert [item.id for item in result.exportable_holdings] == ["cost"]
    assert {(item.category, item.severity) for item in result.findings} == {
        ("incomplete_valuation", "error"),
        ("incomplete_cost_basis", "warning"),
    }


def test_transfer_pairing_requires_opposite_legs():
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            id="out",
            transaction_type="transfer",
            account_id="a",
            currency_code="EUR",
            amount=-100,
            occurred_at=moment,
            provider_metadata_contract={"transfer_id": "t-1"},
            counterparty_account_reference=None,
        ),
        SimpleNamespace(
            id="in",
            transaction_type="transfer",
            account_id="b",
            currency_code="EUR",
            amount=100,
            occurred_at=moment,
            provider_metadata_contract={"transfer_id": "t-1"},
            counterparty_account_reference=None,
        ),
    ]
    assert validate_transaction_stream(rows) == []


def test_transfer_pairing_reads_provider_id_from_metadata_fields():
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            id="out-fields",
            transaction_type="transfer",
            account_id="a",
            currency_code="EUR",
            amount=-100,
            occurred_at=moment,
            provider_metadata_contract={
                "fields": {"transferReference": "t-fields"}
            },
            counterparty_account_reference=None,
        ),
        SimpleNamespace(
            id="in-fields",
            transaction_type="transfer",
            account_id="b",
            currency_code="EUR",
            amount=100,
            occurred_at=moment,
            provider_metadata_contract={
                "fields": {"transferReference": "t-fields"}
            },
            counterparty_account_reference=None,
        ),
    ]

    assert validate_transaction_stream(rows) == []


def test_security_transfer_requires_quantity():
    row = SimpleNamespace(
        id="security-transfer-1",
        transaction_type="transfer",
        security_id="security-1",
        quantity=None,
        amount=100,
        currency_code="EUR",
    )

    findings = validate_activity_semantics(row)

    assert len(findings) == 1
    assert findings[0].category == "incomplete_transaction"
    assert findings[0].severity == "error"
    assert "quantity" in findings[0].message


def test_transfer_requires_non_zero_amount():
    row = SimpleNamespace(
        id="transfer-zero-1",
        transaction_type="transfer",
        amount=0,
        currency_code="EUR",
    )

    findings = validate_activity_semantics(row)

    assert len(findings) == 1
    assert findings[0].category == "incomplete_transaction"
    assert findings[0].severity == "error"
    assert "non-zero" in findings[0].message


@pytest.mark.parametrize("amount", ["NaN", "not-a-number"])
def test_transfer_rejects_non_numeric_amount(amount):
    row = SimpleNamespace(
        id="transfer-invalid-1",
        transaction_type="transfer",
        amount=amount,
        currency_code="EUR",
    )

    findings = validate_activity_semantics(row)

    assert len(findings) == 1
    assert findings[0].category == "incomplete_transaction"
    assert "non-zero" in findings[0].message


def test_transfer_row_adapter_uses_shared_stream_validator():
    findings = validate_transfer_rows(
        [
            (
                "tx-1",
                "account-1",
                "Brokerage",
                -100,
                "EUR",
                datetime(2026, 1, 1, tzinfo=UTC),
                "Transfer out",
            )
        ]
    )

    assert len(findings) == 1
    assert findings[0].category == "unbalanced_transfer"
    assert findings[0].record_id == "tx-1"


def test_transfer_row_adapter_keeps_missing_amount_finding():
    findings = validate_transfer_rows(
        [
            (
                "tx-missing-amount",
                "account-1",
                "Brokerage",
                None,
                "EUR",
                datetime(2026, 1, 1, tzinfo=UTC),
                "Transfer out",
            )
        ]
    )

    assert {item.category for item in findings} == {
        "incomplete_transaction",
        "unbalanced_transfer",
    }
    assert any(item.record_id == "tx-missing-amount" for item in findings)
    assert any(
        "transfer is missing a non-zero amount" in item.message
        for item in findings
    )


def test_transfer_row_adapter_pairs_lightweight_query_rows():
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    findings = validate_transfer_rows(
        [
            ("out", "account-a", "Broker", -100, "EUR", moment, "out"),
            ("in", "account-b", "Broker", 100, "EUR", moment, "in"),
        ]
    )

    assert findings == []


def test_trade_without_quantity_is_blocking():
    row = SimpleNamespace(
        id="tx-1",
        transaction_type="purchase",
        quantity=None,
        unit_price=10,
    )
    findings = validate_transaction_stream([row])
    assert findings[0].category == "incomplete_transaction"
    assert findings[0].severity == "error"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", -1, "quantity"),
        ("quantity", "not-a-number", "quantity"),
        ("unit_price", -1, "unit price"),
        ("unit_price", "not-a-number", "unit price"),
    ],
)
def test_trade_rejects_invalid_numeric_fields(field, value, message):
    row = SimpleNamespace(
        id="trade-invalid-number",
        transaction_type="purchase",
        quantity=1,
        unit_price=10,
        security_id="security-1",
        amount=-10,
        currency_code="EUR",
    )
    setattr(row, field, value)

    findings = validate_activity_semantics(row)

    assert len(findings) == 1
    assert findings[0].category == "invalid_activity_semantics"
    assert findings[0].severity == "error"
    assert message in findings[0].message


def test_transaction_stream_keeps_multiple_trade_findings_on_one_record():
    row = SimpleNamespace(
        id="trade-2",
        transaction_type="purchase",
        quantity=None,
        unit_price=0,
        amount=0,
    )

    findings = validate_transaction_stream([row])

    assert {(item.category, item.severity) for item in findings} == {
        ("incomplete_transaction", "error"),
        ("zero_cost_transaction", "warning"),
    }


def test_trade_requires_security_and_valid_currency_when_fields_are_present():
    row = SimpleNamespace(
        id="trade-3",
        transaction_type="purchase",
        security_id=None,
        quantity=1,
        unit_price=10,
        currency_code="EURO",
        amount=-10,
    )

    findings = validate_activity_semantics(row)

    assert {(item.category, item.severity) for item in findings} == {
        ("incomplete_transaction", "error"),
        ("invalid_activity_semantics", "error"),
    }


def test_activity_rejects_non_ascii_currency_code():
    row = SimpleNamespace(
        id="currency-unicode-1",
        transaction_type="deposit",
        amount=10,
        currency_code="€UR",
    )

    findings = validate_activity_semantics(row)

    assert len(findings) == 1
    assert findings[0].category == "invalid_activity_semantics"
    assert "ISO-4217" in findings[0].message


def test_trade_with_cross_currency_security_requires_fx_rate():
    row = SimpleNamespace(
        id="trade-fx-1",
        transaction_type="purchase",
        security_id="security-1",
        quantity=1,
        unit_price=10,
        amount=-10,
        currency_code="EUR",
        security_currency_code="USD",
        fx_rate=None,
    )

    findings = validate_activity_semantics(row)

    assert findings[0].category == "invalid_activity_semantics"
    assert findings[0].severity == "warning"
    assert "FX rate" in findings[0].message


def test_fee_requires_positive_fee_amount():
    row = SimpleNamespace(
        id="fee-1",
        transaction_type="fee",
        amount=-1,
        currency_code="EUR",
        fee_amount=0,
        fee_currency_code="EUR",
    )

    findings = validate_activity_semantics(row)

    assert findings[0].category == "invalid_activity_semantics"
    assert findings[0].severity == "error"


def test_exportable_activity_requires_stable_external_id_when_available():
    row = SimpleNamespace(
        id="activity-1",
        transaction_type="deposit",
        amount=100,
        currency_code="EUR",
        external_transaction_id="",
    )

    findings = validate_activity_semantics(row)

    assert len(findings) == 1
    assert findings[0].category == "invalid_activity_semantics"
    assert findings[0].severity == "error"
    assert "stable external transaction ID" in findings[0].message


def test_quantity_event_without_ratio_is_insufficient_evidence():
    row = SimpleNamespace(
        id="split-1",
        transaction_type="split",
        provider_metadata_contract={"event": "split"},
    )

    findings = validate_activity_semantics(row)

    assert findings[0].category == "invalid_activity_semantics"
    assert findings[0].severity == "warning"


def test_zero_cost_trade_is_a_shared_semantic_warning():
    row = SimpleNamespace(
        id="trade-1",
        transaction_type="purchase",
        quantity=10,
        unit_price=0,
        amount=0,
    )

    findings = validate_activity_semantics(row)

    assert findings[0].category == "zero_cost_transaction"
    assert findings[0].severity == "warning"


def test_missing_wealthfolio_assets_match_by_isin_then_ticker():
    canonical = [
        SimpleNamespace(id="security-isin", isin="US0378331005", ticker="ACME"),
        SimpleNamespace(id="security-ticker", isin=None, ticker="VWCE"),
    ]
    remote = (
        {"isin": "US0378331005", "symbol": "ACME"},
        {"symbol": "OTHER"},
    )

    assert missing_wealthfolio_assets(canonical, remote) == ("security-ticker",)


@pytest.mark.asyncio
async def test_destination_probe_returns_remote_parity_inputs():
    class Client:
        async def authenticate(self):
            return True

        async def get_accounts(self):
            return [{"id": "remote-account"}]

        async def get_assets(self):
            return [{"id": "remote-asset"}]

        async def get_all_activities(self, account_id):
            return [{"sourceRecordId": "tx-1", "accountId": account_id}]

    result = await probe_wealthfolio_destination(Client())

    assert isinstance(result, WealthfolioDestinationProbe)
    assert result.status == "ready"
    assert result.accounts == ({"id": "remote-account"},)
    assert result.assets == ({"id": "remote-asset"},)

    result = await probe_wealthfolio_destination(
        Client(), include_activities=True
    )
    assert result.activities["remote-account"][0]["sourceRecordId"] == "tx-1"


@pytest.mark.asyncio
async def test_destination_probe_classifies_timeout_without_leaking_error():
    class Client:
        async def authenticate(self):
            raise TimeoutError

    result = await probe_wealthfolio_destination(Client())

    assert result.status == "unavailable"
    assert result.reason == "probe timeout"


@pytest.mark.asyncio
async def test_destination_probe_classifies_auth_rejection() -> None:
    class Client:
        async def authenticate(self):
            return False

    result = await probe_wealthfolio_destination(Client())

    assert result.status == "unauthorized"
    assert result.reason == "authentication rejected"
    assert result.accounts == ()
    assert result.assets == ()


@pytest.mark.asyncio
async def test_destination_probe_classifies_remote_request_failure() -> None:
    from finance_sync.exporter.wealthfolio.client import WealthfolioClientError

    class Client:
        async def authenticate(self):
            message = "remote payload must not escape"
            raise WealthfolioClientError(message)

    result = await probe_wealthfolio_destination(Client())

    assert result.status == "unavailable"
    assert result.reason == "destination request failed"
    assert "payload" not in (result.reason or "")
