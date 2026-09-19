from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from finance_sync.exporter.wealthfolio.extensions import (
    build_extension_payload,
)


def test_extension_payload_reports_coverage_without_inventing_data() -> None:
    account = MagicMock(
        id="account-1",
        name="Broker",
        currency_code="EUR",
        provider_metadata={
            "goals": [{"sourceRecordId": "goal-1", "target": "10000"}]
        },
    )

    payload = build_extension_payload(accounts=[account])

    assert payload["sourceSystem"] == "FINANCE_SYNC"
    assert payload["datasets"]["portfolios"][0]["accountIds"] == ["account-1"]
    assert payload["coverage"]["goals"] == {
        "status": "available",
        "records": 1,
    }
    assert payload["coverage"]["spending"]["status"] == "unavailable"


def test_extension_payload_accepts_structured_p4_records() -> None:
    account = MagicMock(
        id="account-2",
        name="Bank",
        currency_code="EUR",
        provider_metadata={
            "allocations": {
                "sourceRecordId": "allocation-1",
                "targetWeight": 0.6,
            },
            "spending": [
                {"sourceRecordId": "expense-1", "category": "housing"}
            ],
            "notes": {"sourceRecordId": "note-1", "text": "review"},
        },
    )

    payload = build_extension_payload(accounts=[account])

    assert payload["coverage"]["allocations"]["records"] == 1
    assert payload["datasets"]["allocations"][0]["accountId"] == "account-2"
    assert payload["datasets"]["spending"][0]["category"] == "housing"
    assert payload["datasets"]["notes"][0]["sourceSystem"] == "FINANCE_SYNC"


def test_extension_payload_preserves_transaction_lifecycle_events() -> None:
    transaction = MagicMock(
        id="transaction-1",
        external_transaction_id="provider-1",
        account_id="account-1",
        merchant_name="Shop",
        merchant_id=None,
        merchant_category_code="5411",
        cashflow_bucket=None,
        cashflow_suggestion=None,
        description="Shop",
        splits=[],
        lifecycle_events=[
            MagicMock(
                id="event-1",
                event_type="refund",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
                actor="user-1",
                provenance="user_override",
                payload={"amount": "2.00"},
            )
        ],
    )

    payload = build_extension_payload(accounts=[], transactions=[transaction])

    assert payload["coverage"]["events"] == {
        "status": "available",
        "records": 1,
    }
    assert payload["datasets"]["events"][0]["eventType"] == "refund"
