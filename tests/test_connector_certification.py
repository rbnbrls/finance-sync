"""Certification matrix contract tests using synthetic fixtures only."""

from datetime import date

import pytest

from finance_sync.services.connector_certification import (
    CertificationError,
    canonical_fixture_hash,
    validate_certification,
)

CAPABILITIES = ("accounts", "transactions", "holdings", "securities", "fx")


def _entry(**overrides: object) -> dict[str, object]:
    tests = dict.fromkeys(
        ("contract", "retry", "idempotency", "security"), True
    )
    capabilities = {
        name: {"status": "certified", "tests": tests.copy()}
        for name in CAPABILITIES
    }
    value: dict[str, object] = {
        "name": "demo",
        "version": "1.2.3",
        "fixture_date": "2026-08-01",
        "certification_date": "2026-08-15",
        "test_commit": "abc123",
        "expires_at": "2027-08-15",
        "capabilities": capabilities,
        "synthetic_data_only": True,
        "fixture_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # SHA256 of empty string
    }
    value.update(overrides)
    if "fixture_hash" not in overrides:
        value["fixture_hash"] = canonical_fixture_hash(value)
    return value


def test_validates_every_capability_and_required_test_type() -> None:
    result = validate_certification(
        {"connectors": [_entry()]}, "demo", "1.2.3", today=date(2026, 8, 30)
    )
    assert result["status"] == "certified"
    assert set(result["capabilities"]) == set(CAPABILITIES)
    assert result["test_commit"] == "abc123"


def test_missing_capability_or_test_blocks_certification() -> None:
    entry = _entry()
    capabilities = dict(entry["capabilities"])
    del capabilities["fx"]
    entry["capabilities"] = capabilities
    with pytest.raises(CertificationError, match="capability_missing"):
        validate_certification({"connectors": [entry]}, "demo", "1.2.3")


def test_expired_certification_blocks_release_promotion() -> None:
    with pytest.raises(CertificationError, match="certification_expired"):
        validate_certification(
            {"connectors": [_entry(expires_at="2026-08-29")]},
            "demo",
            "1.2.3",
            today=date(2026, 8, 30),
        )


def test_real_credentials_are_rejected() -> None:
    with pytest.raises(CertificationError, match="credentials_forbidden"):
        validate_certification(
            {"connectors": [_entry(credentials={"api_key": "secret"})]},
            "demo",
            "1.2.3",
        )
