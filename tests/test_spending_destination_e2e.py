"""Local provider-to-destination E2E contract for canonical spending.

The destination stores below model the durable identity contract of each
native adapter.  They are deliberately in-process, so this test is useful in
the normal unit suite while still exercising the complete local chain:
provider DTO -> connector transform -> canonical transaction -> native
destination payload -> replay-safe destination write.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import RawTransaction, TransactionSplitData
from finance_sync.exporter.actual_budget.transaction_mapper import (
    map_transaction as map_actual,
)
from finance_sync.exporter.firefly.transaction_mapper import (
    map_transaction as map_firefly,
)
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    map_transaction_to_wf_row,
)
from finance_sync.exporter.ynab.transaction_mapper import (
    map_transaction as map_ynab,
)


class _FixtureProvider(Connector):
    @property
    def name(self) -> str:
        return "fixture-bank"

    async def authenticate(self) -> None:
        return None

    async def fetch_accounts(self) -> list[Any]:
        return []

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        return []


def _canonical_transactions() -> list[Any]:
    raw = [
        RawTransaction(
            external_transaction_id="e2e-payment",
            external_account_id="source-account",
            amount=Decimal("-12.34"),
            currency_code="EUR",
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
            booked_at=datetime(2026, 1, 3, tzinfo=UTC),
            transaction_type="payment",
            status="booked",
            merchant_name="Example Shop",
            merchant_category_code="5411",
            cashflow_bucket="expense",
        ),
        RawTransaction(
            external_transaction_id="e2e-income",
            external_account_id="source-account",
            amount=Decimal("100.00"),
            currency_code="EUR",
            occurred_at=datetime(2026, 1, 4, tzinfo=UTC),
            transaction_type="deposit",
            status="booked",
            cashflow_bucket="income",
        ),
        RawTransaction(
            external_transaction_id="e2e-transfer",
            external_account_id="source-account",
            amount=Decimal("-25.00"),
            currency_code="EUR",
            occurred_at=datetime(2026, 1, 5, tzinfo=UTC),
            transaction_type="transfer",
            status="booked",
            counterparty_account_reference="destination-account",
            cashflow_bucket="transfer",
        ),
    ]
    canonical = _FixtureProvider.transform_transactions(
        _FixtureProvider.__new__(_FixtureProvider), raw
    )
    result: list[Any] = []
    for index, item in enumerate(canonical):
        result.append(
            SimpleNamespace(
                id=f"canonical-{index}",
                provider_key=item.provider_key,
                external_transaction_id=item.external_transaction_id,
                account_id="canonical-account",
                occurred_at=item.occurred_at,
                booked_at=item.booked_at,
                amount=item.amount,
                currency_code=item.currency_code,
                status=item.status,
                transaction_type=item.transaction_type,
                description=item.description or item.transaction_type,
                merchant_name=item.merchant_name,
                merchant_category_code=item.merchant_category_code,
                cashflow_bucket=item.cashflow_bucket,
                cashflow_suggestion=(
                    {"value": "groceries", "source": "merchant_mapping"}
                    if index == 0
                    else None
                ),
                counterparty_account_reference=item.counterparty_account_reference,
                splits=(
                    [
                        TransactionSplitData(
                            amount=Decimal("-7.00"),
                            currency_code="EUR",
                            destination="food",
                        )
                    ]
                    if index == 0
                    else []
                ),
                quantity=None,
                unit_price=None,
                fee_amount=None,
                fee_currency_code=None,
                amount_in_base=None,
                base_currency_code=None,
                fx_rate=None,
                security_id=None,
                provider_fingerprint=None,
                revision=1,
            )
        )
    return result


def _payload_for(destination: str, transaction: Any) -> dict[str, Any]:
    if destination == "actual":
        return map_actual(
            transaction,
            ab_account_name="Checking",
            category_name="Groceries",
        )
    if destination == "firefly":
        return map_firefly(transaction, account_name="Checking")
    if destination == "ynab":
        return map_ynab(
            transaction,
            account_id="ynab-checking",
            category_id="ynab-groceries",
            transfer_account_id=(
                "ynab-savings"
                if transaction.transaction_type == "transfer"
                else None
            ),
        )
    return map_transaction_to_wf_row(transaction)


def _identity(destination: str, payload: dict[str, Any]) -> str:
    if destination == "actual":
        return str(payload["imported_id"])
    if destination == "firefly":
        return str(payload["external_id"])
    if destination == "ynab":
        return str(payload["import_id"])
    return str(payload["idempotencyKey"])


def _amount(destination: str, payload: dict[str, Any]) -> Decimal:
    if destination == "actual":
        return Decimal(payload["amount"]) / 100
    if destination == "ynab":
        return Decimal(payload["amount"]) / 1000
    return Decimal(str(payload["amount"]))


@pytest.mark.parametrize(
    "destination", ["actual", "firefly", "ynab", "wealthfolio"]
)
def test_full_local_destination_pipeline_is_content_and_replay_safe(
    destination: str,
) -> None:
    store: dict[str, dict[str, Any]] = {}
    payloads = [
        _payload_for(destination, transaction)
        for transaction in _canonical_transactions()
    ]

    for payload in payloads + payloads:
        key = _identity(destination, payload)
        store.setdefault(key, payload)

    assert len(store) == 3
    expected_outflow = (
        Decimal("-12.34")
        if destination in {"actual", "ynab"}
        else Decimal("12.34")
    )
    assert _amount(destination, payloads[0]) == expected_outflow
    assert _amount(destination, payloads[1]) == Decimal(100)
    expected_transfer = (
        Decimal(-25) if destination in {"actual", "ynab"} else Decimal(25)
    )
    assert _amount(destination, payloads[2]) == expected_transfer

    first = payloads[0]
    if destination == "actual":
        assert first["category"] == "Groceries"
        assert first["splits"][0]["amount"] == -700
    elif destination == "firefly":
        assert first["category_name"] == "groceries"
        assert first["canonical_splits"][0]["destination"] == "food"
    elif destination == "ynab":
        assert first["category_id"] == "ynab-groceries"
        assert first["subtransactions"][0]["category_id"] == "food"
        assert "transfer_account_id" in payloads[2]
    else:
        assert (
            first["metadata"]["financeSync"]["categorySuggestion"]["value"]
            == "groceries"
        )
        assert first["metadata"]["financeSync"]["splitCount"] == 1
