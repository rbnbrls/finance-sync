"""Provider-neutral spending contract across native destination adapters."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import RawTransaction
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


class _Provider(Connector):
    @property
    def name(self) -> str:
        return "fixture-provider"

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


def test_equivalent_canonical_spending_reaches_each_native_projection() -> None:
    raw = RawTransaction(
        external_transaction_id="payment-1",
        external_account_id="account-1",
        amount=Decimal("-12.34"),
        currency_code="EUR",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        transaction_type="payment",
        status="booked",
        original_type="PAYMENT",
        original_status="BOOKED",
        merchant_name="Example Shop",
        merchant_category_code="5411",
    )
    canonical = _Provider.transform_transactions(
        _Provider.__new__(_Provider), [raw]
    )[0]
    transaction = SimpleNamespace(
        id="canonical-1",
        provider_key="fixture-provider",
        external_transaction_id=canonical.external_transaction_id,
        occurred_at=canonical.occurred_at,
        amount=canonical.amount,
        currency_code=canonical.currency_code,
        status=canonical.status,
        merchant_name=canonical.merchant_name,
        merchant_category_code=canonical.merchant_category_code,
        description=canonical.description or "Example Shop",
        transaction_type=canonical.transaction_type,
        counterparty_account_reference=None,
        splits=None,
        account_id="account-1",
        quantity=None,
        unit_price=None,
        fee_amount=None,
        fee_currency_code=None,
        amount_in_base=None,
        base_currency_code=None,
        fx_rate=None,
        security_id=None,
        provider_fingerprint=None,
        booked_at=canonical.booked_at,
        revision=1,
    )
    transaction_any: Any = transaction
    transaction.splits = [
        SimpleNamespace(
            amount=Decimal("-7.00"),
            category_suggestion="Groceries",
            destination="food",
        )
    ]
    actual = map_actual(
        transaction_any,
        ab_account_name="Checking",
        category_name="Groceries",
    )
    firefly = map_firefly(transaction_any, account_name="Checking")
    ynab = map_ynab(transaction_any, account_id="ynab-account")
    wealthfolio = map_transaction_to_wf_row(transaction_any)

    assert actual["payee"] == "Example Shop"
    assert actual["category"] == "Groceries"
    assert actual["splits"][0]["amount"] == -700
    assert firefly["description"] == "Example Shop"
    assert firefly["category_name"] == "5411"
    assert ynab["payee_name"] == "Example Shop"
    assert ynab["amount"] == -12340
    assert wealthfolio["activityType"] == "FEE"
    assert wealthfolio["sourceRecordId"] == "payment-1"
