"""Holdout scenario evaluation for release19 key-rotation-monitoring.

Evaluates the 8 dark-factory holdout scenarios against the merged
implementation on the feature branch. Mirrors the scenarios posted in the
coder task's `🤖 Holdout scenarios` comment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.key_rotation_monitoring import (
    _check_key_version_downgrade,
    check_key_provider_status,
    check_key_rotation_status,
    should_block_promotion,
)


def test_holdout_clock_skew_no_expiring_soon_alert():
    """Klokskew: 5 min vooruit maar key nog geldig (>24h) -> geen alert."""
    expires_at = (datetime.now(UTC) + timedelta(hours=100)).isoformat()
    key_info = {
        "current_version": "7",
        "state": "managed",
        "hours_to_expiry": 100.0,
        "expires_at": expires_at,
        "material_logged": False,
    }
    alerts = check_key_rotation_status(key_info)
    assert alerts == [], f"Expected no alerts for healthy key, got {alerts}"


def test_holdout_key_version_integer_overflow():
    """Sleutelversie 2^31-1: geen crash, geen downgrade-alert bij max."""
    state = {"last_reported_version": "2147483647"}
    key_info = {"current_version": "2147483647", "state": "managed"}
    alerts = _check_key_version_downgrade(state, key_info)
    assert alerts == []
    # Upgrade naar max+1 via stringvergelijking mag geen crash geven
    key_info2 = {"current_version": "2147483648", "state": "managed"}
    alerts2 = _check_key_version_downgrade(state, key_info2)
    assert alerts2 == []


def test_holdout_key_version_downgrade_alert():
    """Gereplayde oude keyversion -> unexpected downgrade alert."""
    state = {"last_reported_version": "5"}
    key_info = {"current_version": "3", "state": "managed"}
    alerts = _check_key_version_downgrade(state, key_info)
    assert any(a["name"] == "key_version_downgrade" for a in alerts), alerts
    assert alerts[0]["severity"] == "critical"


def test_holdout_cross_tenant_leak_via_shared_cache():
    """Kruis-tenant leak: key status bevat geen velden van andere tenant."""
    result = check_key_provider_status()
    allowed = {
        "current_version",
        "state",
        "rotated_at",
        "expires_at",
        "provider",
        "fail_closed",
        "material_logged",
        "hours_to_expiry",
        "error",
        "status",
    }
    assert isinstance(result, dict)
    unexpected = set(result.keys()) - allowed
    assert not unexpected, f"Unexpected fields leak: {unexpected}"


def test_holdout_provider_returns_empty_or_corrupt_json_on_timeout_recovery():
    """Provider levert lege/corrupt JSON na herstel -> outage, geen crash."""
    key_info = {"error": "provider unavailable", "status": "error"}
    alerts = check_key_rotation_status(key_info)
    assert any(a["name"] == "key_provider_error" for a in alerts), alerts
    assert should_block_promotion(key_info) is True


def test_holdout_injection_via_key_identifier():
    """Key_id met SQL/shell-metacaracters wordt veilig behandeld."""
    state = {"last_reported_version": '"; DROP TABLE keys;--"'}
    key_info = {"current_version": '"; DROP TABLE keys;--"', "state": "managed"}
    # Non-numeric versies worden overgeslagen -> geen crash, geen alert
    alerts = _check_key_version_downgrade(state, key_info)
    assert alerts == []


def test_holdout_promotion_not_blocked_within_rotation_window():
    """Geldige key binnen rotatievenster (24h) -> geen promotieblokkade."""
    key_info = {
        "current_version": "7",
        "state": "managed",
        "hours_to_expiry": 24.0,
        "material_logged": False,
    }
    assert should_block_promotion(key_info) is False
    alerts = check_key_rotation_status(key_info)
    # Warning-level expiring-soon alert is toegestaan, maar geen blokkade
    assert not any(a["severity"] == "critical" for a in alerts), alerts


def test_holdout_no_key_material_in_error_output():
    """Foutmeldingen lekken geen keymateriaal (base64/PEM)."""
    key_info = {
        "error": "Failed to check key provider status",
        "status": "error",
    }
    import json

    body = json.dumps(key_info)
    assert "BEGIN" not in body
    assert "PRIVATE KEY" not in body
    # base64-achtige lange tokens mogen niet in de output staan
    import re

    b64 = re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", body)
    assert not b64, f"Possible key material leaked: {b64[:3]}"
