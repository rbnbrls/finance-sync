from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.connectors.base import Connector
from finance_sync.connectors.capabilities import (
    CapabilityAvailability,
    normalize_capabilities,
)
from finance_sync.connectors.models import (
    CanonicalTransactionData,
    RawCardTransaction,
    RawTransaction,
    SourceReference,
    TransactionSplitData,
)
from finance_sync.exporter.capabilities import (
    destination_capabilities,
    native_transaction_projection,
)
from finance_sync.services.destination_reconciliation import (
    reconcile_destination,
)
from finance_sync.services.destination_references import (
    record_destination_reference,
)
from finance_sync.services.spending_classification import (
    MerchantMapping,
    merge_destination_enrichment,
    normalize_merchant_key,
    suggest_category,
)
from finance_sync.sync.stages.transactions import TransactionSyncStage


def test_old_raw_connector_gets_optional_canonical_spending_fields() -> None:
    class LegacyConnector(Connector):
        @property
        def name(self) -> str:
            return "legacy"

        async def authenticate(self) -> None:
            return None

        async def fetch_accounts(self):
            return []

        async def fetch_transactions(
            self, since, *, account_id=None, limit=None
        ):
            return []

    raw = RawTransaction(
        external_transaction_id="tx-1",
        external_account_id="account-1",
        amount=Decimal("-12.50"),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        transaction_type="PAYMENT",
        status="BOOKED",
    )
    canonical = LegacyConnector.transform_transactions(
        LegacyConnector.__new__(LegacyConnector), [raw]
    )[0]
    assert canonical.original_type == "PAYMENT"
    assert canonical.original_status == "BOOKED"
    assert canonical.merchant_name is None


@pytest.mark.parametrize(
    ("transaction_type", "amount", "status"),
    [
        ("payment", Decimal("-12.50"), "booked"),
        ("refund", Decimal("8.00"), "booked"),
        ("fee", Decimal("-1.25"), "booked"),
        ("deposit", Decimal("100.00"), "booked"),
        ("transfer", Decimal("-25.00"), "pending"),
    ],
)
def test_equivalent_provider_transactions_share_canonical_semantics(
    transaction_type: str, amount: Decimal, status: str
) -> None:
    """Provider IDs may differ, but the spending contract must not."""

    class ProviderConnector(Connector):
        def __init__(self, provider: str) -> None:
            super().__init__(SimpleNamespace(provider_type=provider))

        @property
        def name(self) -> str:
            return self.config.provider_type

        async def authenticate(self) -> None:
            return None

        async def fetch_accounts(self):
            return []

        async def fetch_transactions(
            self, since, *, account_id=None, limit=None
        ):
            return []

    common = {
        "external_account_id": "account-1",
        "amount": amount,
        "currency_code": "EUR",
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        "booked_at": datetime(2026, 1, 2, tzinfo=UTC),
        "description": "Canonical fixture",
        "transaction_type": transaction_type,
        "status": status,
        "merchant_name": "Market NL",
        "merchant_country": "NL",
        "merchant_category_code": "5411",
        "original_type": "provider-native-payment",
        "original_status": "SETTLED",
        "fee_amount": Decimal("0.10") if transaction_type == "fee" else None,
        "refund_amount": Decimal("8.00")
        if transaction_type == "refund"
        else None,
        "refund_currency_code": "EUR" if transaction_type == "refund" else None,
        "source_record_hash": "same-source-content",
        "source_references": [
            SourceReference(
                object_type="transaction", external_ids=["source-1"]
            )
        ],
    }
    first = ProviderConnector("bunq").transform_transactions(
        [RawTransaction(external_transaction_id="bunq-1", **common)]
    )[0]
    second = ProviderConnector("other-bank").transform_transactions(
        [RawTransaction(external_transaction_id="other-1", **common)]
    )[0]

    def projection(item: object) -> tuple[object, ...]:
        return (
            item.amount,
            item.currency_code,
            item.occurred_at,
            item.booked_at,
            item.transaction_type,
            item.status,
            item.original_type,
            item.original_status,
            item.merchant_name,
            item.merchant_country,
            item.merchant_category_code,
            item.fee_amount,
            item.refund_amount,
            item.source_record_hash,
        )

    assert projection(first) == projection(second)


def test_card_payment_and_refund_normalise_to_same_card_contract() -> None:
    class CardConnector(Connector):
        @property
        def name(self) -> str:
            return "card-provider"

        async def authenticate(self) -> None:
            return None

        async def fetch_accounts(self):
            return []

        async def fetch_transactions(
            self, since, *, account_id=None, limit=None
        ):
            return []

    raw = RawCardTransaction(
        external_card_transaction_id="card-1",
        external_account_id="account-1",
        amount=Decimal("-20.00"),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        merchant_name="Market NL",
        mcc="5411",
        authorization_type="settlement",
        status="booked",
        refund_amount=Decimal("5.00"),
        refund_currency_code="EUR",
    )
    canonical = CardConnector.transform_card_transactions(
        CardConnector.__new__(CardConnector), [raw]
    )[0]
    assert canonical.amount == Decimal("-20.00")
    assert canonical.authorization_type == "settlement"
    assert canonical.merchant_category_code == "5411"
    assert canonical.refund_amount == Decimal("5.00")


def test_capability_contract_validates_and_discards_invalid_entries() -> None:
    result = normalize_capabilities(
        {"merchant_data": "partial", "broken": "not-a-level"}
    )
    assert (
        result["merchant_data"].availability == CapabilityAvailability.PARTIAL
    )
    assert "broken" not in result


def test_category_mapping_and_merge_protect_destination_state() -> None:
    transaction = SimpleNamespace(
        merchant_name="Acme, B.V.",
        merchant_country="NL",
        merchant_category_code="5411",
        classification_override=None,
        cashflow_suggestion=None,
    )
    key = normalize_merchant_key("Acme, B.V.", "NL")
    suggestion = suggest_category(
        transaction,
        {key: MerchantMapping(key, "Acme", "groceries", "personal")},
    )
    assert suggestion is not None
    assert suggestion.value == "groceries"
    merged = merge_destination_enrichment(
        {"category": "manual", "merchant_name": "Old"},
        {"category": "source", "merchant_name": "New", "mcc": "5411"},
    )
    assert merged["category"] == "manual"
    assert merged["merchant_name"] == "New"
    assert merged["mcc"] == "5411"


def test_destination_projection_and_reconciliation_are_idempotency_friendly() -> (
    None
):
    transaction = SimpleNamespace(
        id="local-1",
        provider_key="bunq",
        external_transaction_id="provider-1",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        amount=Decimal("-10.00"),
        currency_code="EUR",
        status="booked",
        merchant_name="Shop",
        counterparty_name=None,
        description="Shop purchase",
        cashflow_suggestion=None,
        splits=None,
    )
    payload = native_transaction_projection(
        "ynab", transaction, account_name="Checking"
    )
    assert payload["import_id"] == "finance-sync:bunq:provider-1"
    assert "category_assignments" not in destination_capabilities("ynab")
    findings = reconcile_destination(
        [transaction],
        [
            {
                "import_id": payload["import_id"],
                "amount": "-9.00",
                "currency_code": "EUR",
            }
        ],
        destination_type="ynab",
    )
    assert any(item["kind"] == "amount_mismatch" for item in findings)


def test_reconciliation_normalizes_ynab_milliunits_and_firefly_categories() -> (
    None
):
    transaction = SimpleNamespace(
        provider_key="ynab",
        external_transaction_id="tx-2",
        amount=Decimal("-12.34"),
        currency_code="EUR",
        cashflow_bucket="groceries",
    )

    findings = reconcile_destination(
        [transaction],
        [
            {
                "import_id": "ynab:tx-2",
                "amount": -12340,
                "currency_code": "EUR",
                "category_name": "groceries",
            }
        ],
        destination_type="ynab",
    )

    assert findings == []


@pytest.mark.asyncio
async def test_destination_reference_upsert_is_tenant_scoped() -> None:
    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        def __init__(self) -> None:
            self.added = []
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            return Result()

        def add(self, value: object) -> None:
            self.added.append(value)

        async def commit(self) -> None:
            self.committed = True

    session = Session()

    def factory():
        return session

    await record_destination_reference(
        factory,
        tenant_id="tenant-1",
        destination_type="ynab",
        transaction_id="tx-1",
        canonical_key="bunq:provider-1",
        destination_object_id="remote-1",
        idempotency_key="finance-sync:bunq:provider-1",
        source_revision=3,
    )

    reference = session.added[0]
    assert reference.tenant_id == "tenant-1"
    assert reference.destination_object_id == "remote-1"
    assert session.committed


@pytest.mark.asyncio
async def test_destination_enrichment_preserves_user_owned_fields_on_resync() -> (
    None
):
    from finance_sync.sync.persistence import TransactionPersistence

    entity = SimpleNamespace(
        tenant_id="tenant-1",
        merchant_name="Manual merchant",
        description="Manual note",
        classification_override="manual-category",
        revision=4,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=entity),
        flush=AsyncMock(),
    )
    uow = SimpleNamespace(session=session)
    with patch(
        "finance_sync.sync.persistence._add_lifecycle_event"
    ) as lifecycle:
        result = await TransactionPersistence(
            "tenant-1"
        ).apply_destination_enrichment(
            uow,
            "transaction-1",
            {
                "merchant_name": "Fresh merchant",
                "description": "Fresh source description",
                "classification_override": "source-category",
            },
        )

    assert result is entity
    assert entity.merchant_name == "Fresh merchant"
    assert entity.description == "Fresh source description"
    assert entity.classification_override == "manual-category"
    assert entity.revision == 5
    lifecycle.assert_called_once()


@pytest.mark.asyncio
async def test_second_sync_preserves_override_splits_and_appends_lifecycle_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.sync import persistence
    from finance_sync.sync.persistence import TransactionPersistence

    first = CanonicalTransactionData(
        provider_key="bunq",
        external_transaction_id="tx-1",
        external_account_id="account-1",
        amount=Decimal("-20.00"),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        transaction_type="payment",
        status="booked",
        classification_override="manual-food",
        cashflow_suggestion={"value": "groceries", "source": "rule"},
        splits=[
            TransactionSplitData(
                amount=Decimal("-12.00"),
                currency_code="EUR",
                destination="groceries",
            ),
            TransactionSplitData(
                amount=Decimal("-8.00"),
                currency_code="EUR",
                destination="household",
            ),
        ],
    )
    second = first.model_copy(
        update={
            "amount": Decimal("-21.00"),
            "classification_override": "provider-category",
            "cashflow_suggestion": {"value": "shopping", "source": "provider"},
        }
    )
    existing = SimpleNamespace(
        id="tx-local",
        tenant_id="tenant-1",
        revision=1,
        amount=first.amount,
        currency_code="EUR",
        occurred_at=first.occurred_at,
        booked_at=None,
        transaction_type="payment",
        description=None,
        quantity=None,
        unit_price=None,
        fee_amount=None,
        fee_currency_code=None,
        status="booked",
        amount_in_base=None,
        base_currency_code=None,
        fx_rate=None,
        provider_fingerprint=None,
        security_id=None,
        classification_override="manual-food",
        splits=list(first.splits),
        lifecycle_events=["create"],
    )
    repository = SimpleNamespace(
        get_by_external_id=AsyncMock(side_effect=[None, existing])
    )
    session = SimpleNamespace(add=MagicMock(), flush=AsyncMock())
    uow = SimpleNamespace(session=session, transactions=repository)
    monkeypatch.setattr(persistence, "outbox_entity_created", AsyncMock())
    monkeypatch.setattr(persistence, "outbox_entity_updated", AsyncMock())
    lifecycle = MagicMock()
    monkeypatch.setattr(persistence, "_add_lifecycle_event", lifecycle)
    writer = TransactionPersistence("tenant-1")

    created = await writer.persist_transaction(uow, first, "account-1")
    await writer.persist_transaction(uow, second, "account-1")

    assert created is not existing
    assert existing.amount == Decimal("-21.00")
    assert existing.classification_override == "manual-food"
    assert existing.splits == first.splits
    assert existing.lifecycle_events == ["create"]
    assert lifecycle.call_count == 2
    assert lifecycle.call_args.kwargs["provenance"] == "provider_sync"


@pytest.mark.asyncio
async def test_transaction_stage_reports_exact_upsert_and_spending_counts() -> (
    None
):
    from finance_sync.connectors.models import CanonicalTransactionData

    class Writer:
        last_upsert_outcome = {"new": 1, "changed": 1, "unchanged": 0}

        async def persist_transactions_batch(self, *args, **kwargs) -> int:
            return 2

        async def resolve_security_reference(self, *args, **kwargs):
            return None, None

    transactions = [
        CanonicalTransactionData(
            provider_key="bunq",
            external_transaction_id="one",
            external_account_id="account",
            amount=-1,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            transaction_type="payment",
            cashflow_suggestion={
                "value": "groceries",
                "source": "merchant_mapping",
                "confidence": 1,
            },
        ),
        CanonicalTransactionData(
            provider_key="bunq",
            external_transaction_id="two",
            external_account_id="account",
            amount=-2,
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
            transaction_type="payment",
        ),
    ]
    result = await TransactionSyncStage(Writer()).run(
        object(), transactions, account_id="account", provider_type="bunq"
    )
    assert (result.new, result.changed, result.unchanged) == (1, 1, 0)
    assert (result.classified, result.unclassified, result.split) == (1, 1, 0)
