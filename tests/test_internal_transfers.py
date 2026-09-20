from datetime import UTC, datetime
from decimal import Decimal

from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ProviderMetadata,
)
from finance_sync.exporter.actual_budget.exporter import _account_names_by_iban
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    WF_ACTIVITY_TRANSFER_OUT,
    _resolve_activity_type,
)
from finance_sync.services.internal_transfers import (
    bunq_owned_ibans,
    classify_bunq_internal_transfers,
    is_bunq_easy_budgeting_transaction,
)
from finance_sync.services.wealthfolio_preflight import (
    validate_transaction_stream,
)


def _account(external_id: str, name: str, iban: str) -> CanonicalAccountData:
    return CanonicalAccountData(
        provider_key="bunq",
        external_account_id=external_id,
        name=name,
        account_type="checking",
        currency_code="EUR",
        provider_metadata={"iban": iban},
    )


def _transaction(
    *,
    external_id: str,
    account_id: str,
    amount: str,
    counterparty: str,
    transaction_type: str = "payment",
) -> CanonicalTransactionData:
    return CanonicalTransactionData(
        provider_key="bunq",
        external_transaction_id=external_id,
        external_account_id=account_id,
        amount=Decimal(amount),
        currency_code="EUR",
        occurred_at=datetime(2026, 7, 22, 8, 48, tzinfo=UTC),
        transaction_type=transaction_type,
        counterparty_account_reference=counterparty,
    )


def test_easy_budgeting_payment_to_owned_iban_becomes_transfer() -> None:
    accounts = [
        _account("inbox", "Inbox", "NL00 BUNQ 0000 0000 01"),
        _account("fixed", "Vaste lasten", "NL00BUNQ000000000002"),
    ]

    result = classify_bunq_internal_transfers(
        [
            _transaction(
                external_id="top-up",
                account_id="inbox",
                amount="-500.00",
                counterparty="NL00BUNQ000000000002",
            )
        ],
        owned_ibans=bunq_owned_ibans(accounts),
    )

    assert result[0].transaction_type == "transfer"
    assert result[0].classification_source == "bunq_internal_transfer"
    assert result[0].provider_metadata == {
        "internal_transfer": True,
        "internal_transfer_detection": "owned_counterparty_iban",
    }


def test_external_salary_remains_income_like_transaction() -> None:
    accounts = [_account("inbox", "Inbox", "NL00BUNQ000000000001")]
    salary = _transaction(
        external_id="salary",
        account_id="inbox",
        amount="3000.00",
        counterparty="NL00BANK000000000099",
    )

    result = classify_bunq_internal_transfers(
        [salary], owned_ibans=bunq_owned_ibans(accounts)
    )

    assert result == [salary]


def test_existing_transfer_type_is_preserved() -> None:
    accounts = [_account("inbox", "Inbox", "NL00BUNQ000000000001")]
    transfer = _transaction(
        external_id="existing-transfer",
        account_id="inbox",
        amount="-10.00",
        counterparty="NL00BUNQ000000000001",
        transaction_type="transfer",
    )

    result = classify_bunq_internal_transfers(
        [transfer], owned_ibans=bunq_owned_ibans(accounts)
    )

    assert result[0] is transfer


def test_easy_budgeting_allocate_without_counterparty_is_transfer() -> None:
    account = _account("sport", "Sport", "NL00BUNQ000000000001")
    allocation = _transaction(
        external_id="allocate",
        account_id="sport",
        amount="250.00",
        counterparty="",
    ).model_copy(
        update={
            "description": "Automatic top-up of your Sport budget.",
            "original_type": "PAYMENT_ALLOCATE",
        }
    )

    result = classify_bunq_internal_transfers(
        [allocation], owned_ibans=bunq_owned_ibans([account])
    )

    assert result[0].transaction_type == "transfer"
    assert result[0].provider_metadata is not None
    assert result[0].provider_metadata["internal_transfer"] is True
    assert (
        result[0].provider_metadata["internal_transfer_detection"]
        == "bunq_easy_budgeting"
    )
    assert result[0].provider_metadata["easy_budgeting_operation"] == "top_up"
    assert result[0].provider_metadata["easy_budgeting_budget"] == "Sport"
    assert result[0].provider_metadata["internal_transfer_pair_key"]


def test_bunq_generic_automatic_budget_top_up_without_counterparty_is_transfer() -> (
    None
):
    account = _account("outbox", "Outbox", "NL00BUNQ000000000001")
    top_up = _transaction(
        external_id="generic-top-up",
        account_id="outbox",
        amount="-500.00",
        counterparty="",
        transaction_type="other",
    ).model_copy(
        update={
            "description": "Automatic budget top up.",
            "original_type": "BUNQ",
        }
    )

    result = classify_bunq_internal_transfers(
        [top_up], owned_ibans=bunq_owned_ibans([account])
    )

    assert result[0].transaction_type == "transfer"
    assert result[0].provider_metadata["easy_budgeting_operation"] == "top_up"
    assert result[0].provider_metadata["easy_budgeting_budget"] is None


def test_easy_budgeting_rows_are_excluded_from_wealthfolio_projection() -> None:
    transaction = _transaction(
        external_id="remainder",
        account_id="inbox",
        amount="-100.00",
        counterparty="",
        transaction_type="transfer",
    ).model_copy(update={"description": "Remainder of your Inbox budget."})

    assert is_bunq_easy_budgeting_transaction(transaction) is True


def test_normal_bunq_transfer_remains_in_wealthfolio_projection() -> None:
    transaction = _transaction(
        external_id="normal-transfer",
        account_id="inbox",
        amount="-100.00",
        counterparty="NL00BUNQ000000000002",
        transaction_type="transfer",
    )

    assert is_bunq_easy_budgeting_transaction(transaction) is False


def test_internal_transfer_is_neutral_in_both_downstream_mappers() -> None:
    accounts = [
        _account("inbox", "Inbox", "NL00BUNQ000000000001"),
        _account("outbox", "Outbox", "NL00BUNQ000000000002"),
    ]
    transaction = _transaction(
        external_id="remainder",
        account_id="outbox",
        amount="-123.45",
        counterparty="NL00BUNQ000000000001",
    )
    normalized = classify_bunq_internal_transfers(
        [transaction], owned_ibans=bunq_owned_ibans(accounts)
    )[0]

    assert _resolve_activity_type(normalized) == WF_ACTIVITY_TRANSFER_OUT
    assert _account_names_by_iban(accounts, {}) == {
        "NL00BUNQ000000000001": "Inbox",
        "NL00BUNQ000000000002": "Outbox",
    }


def test_cross_account_easy_budgeting_pair_is_not_blocked_per_account() -> None:
    transaction = _transaction(
        external_id="cross-account",
        account_id="inbox",
        amount="-12.00",
        counterparty="",
        transaction_type="transfer",
    ).model_copy(
        update={
            "provider_metadata_contract": ProviderMetadata(
                fields={
                    "transfer_id": "bunq-easy-budgeting:test",
                    "pair_scope": "cross_account",
                }
            )
        }
    )

    assert validate_transaction_stream([transaction]) == []
