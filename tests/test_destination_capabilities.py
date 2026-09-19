from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from finance_sync.exporter.ynab.transaction_mapper import map_transaction


def test_ynab_mapping_uses_native_milliunits_and_transfer_semantics() -> None:
    transaction = SimpleNamespace(
        provider_key="bunq",
        external_transaction_id="p-1",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        amount=Decimal("-12.34"),
        status="booked",
        merchant_name="Shop",
        description="Groceries",
        transaction_type="payment",
        splits=None,
    )
    payload = map_transaction(
        transaction,
        account_id="ynab-account",
        category_id="ynab-category",
    )
    assert payload["amount"] == -12340
    assert payload["category_id"] == "ynab-category"
    assert payload["import_id"] == "finance-sync:bunq:p-1"

    transfer = map_transaction(
        SimpleNamespace(
            **{**transaction.__dict__, "transaction_type": "transfer"}
        ),
        account_id="a",
        category_id="must-not-be-used",
        transfer_account_id="b",
    )
    assert transfer["transfer_account_id"] == "b"
    assert "category_id" not in transfer
