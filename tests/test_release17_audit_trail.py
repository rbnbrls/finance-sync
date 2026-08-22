"""Release 17 audit-trail completeness contracts."""

# pyright: basic

import json
from pathlib import Path

import pytest

from finance_sync.utils.redaction import REDACTED, redact_text
from scripts.audit_trail_completeness import validate_incident, validate_policy


def test_audit_policy_covers_security_and_configuration_changes() -> None:
    policy = json.loads(Path("config/audit-trail-policy.json").read_text())
    example = json.loads(Path("config/incident-audit-example.json").read_text())
    validate_policy(policy)
    validate_incident(example, policy)
    assert policy["read_only"] is True
    assert set(policy["required_fields"]) == {
        "actor", "timestamp", "tenant", "object_type", "action", "redacted_diff"
    }


def test_audit_records_redact_secrets_and_financial_values() -> None:
    value = redact_text("token=api_secret_123456789 amount=125.50")
    assert REDACTED in value
    assert "api_secret_123456789" not in value
    assert "125.50" in value  # value is not an audit field and is not persisted
    policy = json.loads(Path("config/audit-trail-policy.json").read_text())
    invalid = {"records": [{"actor": "x", "timestamp": "x", "tenant": "x", "object_type": "export", "action": "retry", "redacted_diff": {"amount": "[REDACTED]"}}]}
    with pytest.raises(ValueError, match="sensitive field"):
        validate_incident(invalid, policy)


def test_ci_validates_exportable_incident_audit_example() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "audit_trail_completeness.py" in workflow
    assert "incident-audit-example.json" in workflow
