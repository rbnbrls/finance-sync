import json
from pathlib import Path

from finance_sync.connectors.models import RawTransaction


def test_spending_contract_fixtures_cover_optional_and_redacted_data() -> None:
    fixture = json.loads(
        Path("config/spending-contract-fixtures.json").read_text()
    )
    assert fixture["schema_version"] == "1"
    assert "raw_payload" in fixture["privacy"]["redacted_fields"]
    names = {case["name"] for case in fixture["cases"]}
    assert {
        "bunq_payment_with_merchant_and_mcc",
        "legacy_provider_without_spending_capabilities",
        "redacted_attachment_reference",
    } <= names
    for case in fixture["cases"]:
        transaction = RawTransaction.model_validate(case["raw"])
        assert transaction.external_transaction_id
