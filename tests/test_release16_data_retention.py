"""Release 16 data-retention and privacy audit contracts."""

# pyright: basic

import json
from pathlib import Path

import pytest

from finance_sync.utils.redaction import REDACTED, redact_text
from scripts.check_data_retention_policy import validate


def _policy() -> dict:
    return json.loads(Path("config/data-retention-policy.json").read_text())


def test_policy_covers_storage_categories_and_safe_deletion() -> None:
    policy = _policy()
    validate(policy)
    categories = {item["name"]: item for item in policy["categories"]}
    assert categories["credentials"]["deletion"] == "delete_encrypted_envelope_with_connection"
    assert "anonymise" in categories["financial_facts"]["deletion"]
    assert categories["provider_payloads"]["retention_days"] == 0
    assert all(item["tenant_scoped"] for item in policy["categories"])


def test_redaction_and_invalid_policy_are_enforced() -> None:
    clean = redact_text("provider failed token=api_secret_123456789")
    assert REDACTED in clean
    assert "api_secret_123456789" not in clean
    invalid = _policy()
    invalid["categories"] = [item for item in invalid["categories"] if item["name"] != "logs"]
    with pytest.raises(ValueError, match="missing retention categories"):
        validate(invalid)


def test_ci_validates_and_publishes_the_privacy_policy() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "check_data_retention_policy.py" in workflow
    assert "data-retention-policy.json" in workflow
