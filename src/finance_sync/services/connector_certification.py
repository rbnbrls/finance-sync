"""Pure, credential-free certification gates for connector releases."""

# pyright: basic

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any, NoReturn

CAPABILITIES = ("accounts", "transactions", "holdings", "securities", "fx")
REQUIRED_TESTS = ("contract", "retry", "idempotency", "security")


def _contains_secret_like(value: str) -> bool:
    """Return True if the string looks like a secret or credential."""
    vlower = value.lower()
    return vlower.startswith("sk_") or "secret" in vlower


def canonical_fixture_hash(entry: dict[str, Any]) -> str:
    """Return the canonical digest for a certification fixture entry."""
    fixture = {
        key: entry[key]
        for key in ("fixture_date", "test_commit")
        if key in entry
    }
    encoded = json.dumps(fixture, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CertificationError(ValueError):
    """A connector certification matrix cannot authorize promotion."""


def _fail(code: str) -> NoReturn:
    raise CertificationError(code)


def _as_date(value: Any, code: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        _fail(code)


def validate_certification(
    matrix: dict[str, Any],
    provider_key: str,
    version: str,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Validate one matrix entry and return its safe promotion summary.

    The matrix is deliberately data-only: credentials and credential-shaped
    fields are rejected so CI can run against committed synthetic fixtures.
    """
    entry = next(
        (
            item
            for item in matrix.get("connectors", [])
            if isinstance(item, dict)
            and item.get("name") == provider_key
            and item.get("version") == version
        ),
        None,
    )
    if not isinstance(entry, dict):
        _fail("certification_missing")
    # Reject secret-like values before processing metadata.
    for field_value in entry.values():
        if isinstance(field_value, str) and _contains_secret_like(field_value):
            _fail("secret_detected")
    if (
        matrix.get("synthetic_data_only") is not True
        and entry.get("synthetic_data_only") is not True
    ) or entry.get("credentials"):
        _fail("credentials_forbidden")
    if any(key in entry for key in ("api_key", "api_secret", "access_token")):
        _fail("credentials_forbidden")
    for field in (
        "fixture_date",
        "certification_date",
        "test_commit",
        "expires_at",
        "fixture_hash",
    ):
        if not entry.get(field):
            _fail(f"{field}_missing")
    fixture_date = _as_date(entry["fixture_date"], "fixture_date_invalid")
    certification_date = _as_date(
        entry["certification_date"], "certification_date_invalid"
    )
    expires_at = _as_date(entry["expires_at"], "expires_at_invalid")
    fixture_hash = entry.get("fixture_hash")
    if (
        not isinstance(fixture_hash, str)
        or fixture_hash != fixture_hash.lower()
    ):
        _fail("fixture_hash_invalid")
    if fixture_hash != canonical_fixture_hash(entry):
        _fail("fixture_hash_invalid")
    current = today or datetime.now(UTC).date()
    if expires_at < current:
        _fail("certification_expired")
    if certification_date < fixture_date:
        _fail("certification_before_fixture")
    capability_map = entry.get("capabilities")
    if not isinstance(capability_map, dict):
        _fail("capability_matrix_missing")
    for capability in CAPABILITIES:
        capability_entry = capability_map.get(capability)
        if not isinstance(capability_entry, dict):
            _fail("capability_missing")
        if capability_entry.get("status") != "certified":
            _fail(f"capability_not_certified:{capability}")
        tests = capability_entry.get("tests")
        if not isinstance(tests, dict):
            _fail(f"tests_missing:{capability}")
        for test_name in REQUIRED_TESTS:
            if tests.get(test_name) is not True:
                _fail(f"test_failed:{capability}:{test_name}")
    return {
        "provider": provider_key,
        "version": version,
        "status": "certified",
        "fixture_date": fixture_date.isoformat(),
        "certification_date": certification_date.isoformat(),
        "test_commit": str(entry["test_commit"]),
        "expires_at": expires_at.isoformat(),
        "capabilities": list(CAPABILITIES),
        "synthetic_data_only": True,
    }
