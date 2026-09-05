from datetime import UTC, datetime
from types import SimpleNamespace

from finance_sync.services.wealthfolio_preflight import (
    validate_holdings,
    validate_transaction_stream,
)


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
        [_holding(id="bad", market_value=None, price=None), _holding(id="cost", cost_basis=None)]
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
