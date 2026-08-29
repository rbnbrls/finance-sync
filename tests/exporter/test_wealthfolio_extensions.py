from __future__ import annotations

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
