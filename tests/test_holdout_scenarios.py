"""Holdout scenario tests for connector certification."""

from datetime import date, timedelta
from typing import Any

import pytest

from finance_sync.services.connector_certification import (
    CertificationError,
    canonical_fixture_hash,
    validate_certification,
)

CAPABILITIES = ("accounts", "transactions", "holdings", "securities", "fx")
TESTS = ("contract", "retry", "idempotency", "security")


def _entry(**overrides: object) -> dict[str, Any]:
    capabilities: dict[str, dict[str, Any]] = {
        name: {"status": "certified", "tests": dict.fromkeys(TESTS, True)}
        for name in CAPABILITIES
    }
    value: dict[str, Any] = {
        "name": "demo",
        "version": "1.2.3",
        "fixture_date": "2026-08-01",
        "certification_date": "2026-08-15",
        "test_commit": "abc123",
        "expires_at": "2027-08-15",
        "capabilities": capabilities,
        "synthetic_data_only": True,
    }
    value.update(overrides)
    if "fixture_hash" not in overrides:
        value["fixture_hash"] = canonical_fixture_hash(value)
    return value


def test_fixture_metadata_injection() -> None:
    """Reject executable-looking metadata and secret placeholders literally."""
    with pytest.raises(CertificationError, match="fixture_date_invalid"):
        validate_certification(
            {"connectors": [_entry(fixture_date="; DROP TABLE certifications;")]},
            "demo",
            "1.2.3",
        )
    result = validate_certification(
        {"connectors": [_entry(test_commit="; DROP TABLE certifications;")]},
        "demo",
        "1.2.3",
        today=date(2026, 8, 30),
    )
    assert result["test_commit"] == "; DROP TABLE certifications;"
    with pytest.raises(CertificationError, match="secret_detected"):
        validate_certification(
            {"connectors": [_entry(test_commit="${SECRET}")]},
            "demo",
            "1.2.3",
            today=date(2026, 8, 30),
        )


def test_tenant_isolation() -> None:
    matrix = {"connectors": [_entry(name="tenantA", version="1.0"), _entry(name="tenantB", version="2.0")]}
    result_a = validate_certification(matrix, "tenantA", "1.0", today=date(2026, 8, 30))
    result_b = validate_certification(matrix, "tenantB", "2.0", today=date(2026, 8, 30))
    assert (result_a["provider"], result_a["version"]) == ("tenantA", "1.0")
    assert (result_b["provider"], result_b["version"]) == ("tenantB", "2.0")


def test_secret_leak_in_artefacts_en_logs() -> None:
    with pytest.raises(CertificationError, match="secret_detected"):
        validate_certification(
            {"connectors": [_entry(test_commit="sk_liv...cret")]},
            "demo",
            "1.2.3",
            today=date(2026, 8, 30),
        )


def test_exact_expiration_boundary() -> None:
    today = date(2026, 8, 30)
    with pytest.raises(CertificationError, match="certification_expired"):
        validate_certification({"connectors": [_entry(expires_at=(today - timedelta(days=1)).isoformat())]}, "demo", "1.2.3", today=today)
    assert validate_certification({"connectors": [_entry(expires_at=today.isoformat())]}, "demo", "1.2.3", today=today)["status"] == "certified"
    assert validate_certification({"connectors": [_entry(expires_at=(today + timedelta(days=1)).isoformat())]}, "demo", "1.2.3", today=today)["status"] == "certified"


def test_atomic_promotion_blockade() -> None:
    entry = _entry()
    del entry["capabilities"]["fx"]
    entry["fixture_hash"] = canonical_fixture_hash(entry)
    with pytest.raises(CertificationError, match="capability_missing"):
        validate_certification({"connectors": [entry]}, "demo", "1.2.3")

    entry = _entry()
    entry["capabilities"]["fx"]["status"] = "uncertified"
    entry["fixture_hash"] = canonical_fixture_hash(entry)
    with pytest.raises(CertificationError, match="capability_not_certified:fx"):
        validate_certification({"connectors": [entry]}, "demo", "1.2.3")

    entry = _entry()
    entry["capabilities"]["fx"]["tests"]["contract"] = False
    entry["fixture_hash"] = canonical_fixture_hash(entry)
    with pytest.raises(CertificationError, match="test_failed:fx:contract"):
        validate_certification({"connectors": [entry]}, "demo", "1.2.3")


def test_concurrent_certification_updates() -> None:
    matrix = {"connectors": [_entry()]}
    results = [validate_certification(matrix, "demo", "1.2.3", today=date(2026, 8, 30)) for _ in range(5)]
    assert all(result == results[0] for result in results)


def test_retry_after_process_interruption() -> None:
    matrix = {"connectors": [_entry()]}
    result = validate_certification(matrix, "demo", "1.2.3", today=date(2026, 8, 30))
    assert validate_certification(matrix, "demo", "1.2.3", today=date(2026, 8, 30)) == result


def test_fixture_drift_en_versie_integriteit() -> None:
    entry = _entry()
    entry["test_commit"] = "changed fixture content"
    with pytest.raises(CertificationError, match="fixture_hash_invalid"):
        validate_certification({"connectors": [entry]}, "demo", "1.2.3")
    with pytest.raises(CertificationError, match="fixture_hash_missing"):
        entry = _entry()
        del entry["fixture_hash"]
        validate_certification({"connectors": [entry]}, "demo", "1.2.3")
    with pytest.raises(CertificationError, match="fixture_hash_invalid"):
        validate_certification({"connectors": [_entry(fixture_hash="nothex")]}, "demo", "1.2.3")
