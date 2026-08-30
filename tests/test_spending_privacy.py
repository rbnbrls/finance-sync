from finance_sync.exporter.wealthfolio.extensions import build_extension_payload
from finance_sync.services.spending_privacy import redact_destination_metadata


def test_nested_destination_metadata_redacts_sensitive_fields_by_default() -> None:
    value = redact_destination_metadata(
        {
            "merchant": "Shop",
            "iban": "NL00BANK0123456789",
            "nested": {
                "raw_payload": {"secret": "value"},
                "pan": "4111111111111111",
                "attachment_content": "binary",
            },
        }
    )

    assert value == {"merchant": "Shop", "nested": {}}


def test_wealthfolio_extension_does_not_forward_raw_provider_payload() -> None:
    account = type(
        "Account",
        (),
        {
            "id": "account-1",
            "name": "Checking",
            "currency_code": "EUR",
            "provider_metadata": {
                "spending": {
                    "merchant": "Shop",
                    "raw_payload": {"iban": "NL00BANK0123456789"},
                    "attachment_content": "binary",
                }
            },
        },
    )()

    payload = build_extension_payload(accounts=[account])
    spending = payload["datasets"]["spending"][0]
    assert spending == {
        "merchant": "Shop",
        "accountId": "account-1",
        "sourceSystem": "FINANCE_SYNC",
    }
