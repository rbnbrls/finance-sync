"""Tests for key rotation monitoring functionality."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from scripts.key_rotation_monitoring import (
    _check_key_version_downgrade,
    build_key_issue_body,
    build_key_marker,
    check_key_provider_status,
    check_key_rotation_status,
    should_block_promotion,
)


def test_build_key_marker():
    """Test that the key marker is built correctly."""
    test_date = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    marker = build_key_marker(test_date)
    assert marker == "<!-- key-rotation-monitor:2026-08-28 -->"


def test_check_key_provider_status_with_config():
    """Test key provider status check by verifying it returns expected structure."""
    # Just test that the function returns a dict with expected keys when config exists
    # We'll test in the actual repo directory where we know the config file exists
    result = check_key_provider_status()

    # Should return a dict with key information
    assert isinstance(result, dict)
    # Should have either error or key info
    if "error" not in result:
        assert "current_version" in result
        assert "state" in result
        assert "hours_to_expiry" in result


def test_check_key_provider_status_error():
    """Test key provider status check when config is missing."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("os.getcwd", return_value="/nonexistent"),
        patch("os.path.exists", return_value=False),
    ):
        result = check_key_provider_status()

        assert "error" in result
        assert result["status"] == "error"


def test_check_key_rotation_status_approaching_expiry():
    """Test key rotation status check when key is approaching expiry."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 20,  # Less than default 24 hours
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": False,
    }

    with patch.dict(
        "os.environ", {"KEY_ROTATION_ALERT_BEFORE_EXPIRY_HOURS": "24"}
    ):
        alerts = check_key_rotation_status(key_info)

        assert len(alerts) == 1
        assert alerts[0]["name"] == "key_approaching_expiry"
        assert alerts[0]["severity"] == "warning"
        assert "20.0 hours" in alerts[0]["detail"]


def test_check_key_rotation_status_critical_expiry():
    """Test key rotation status check when key is critically close to expiry."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 0.5,  # 30 minutes
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": False,
    }

    with patch.dict(
        "os.environ", {"KEY_ROTATION_ALERT_BEFORE_EXPIRY_HOURS": "24"}
    ):
        alerts = check_key_rotation_status(key_info)

        assert len(alerts) == 1
        assert alerts[0]["name"] == "key_approaching_expiry"
        assert alerts[0]["severity"] == "critical"
        assert "0.5 hours" in alerts[0]["detail"]


def test_check_key_rotation_status_no_alerts():
    """Test key rotation status check when no alerts are needed."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 100,  # Well beyond alert threshold
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": False,
    }

    with patch.dict(
        "os.environ", {"KEY_ROTATION_ALERT_BEFORE_EXPIRY_HOURS": "24"}
    ):
        alerts = check_key_rotation_status(key_info)

        assert len(alerts) == 0


def test_check_key_rotation_status_with_error():
    """Test key rotation status check when key info contains error."""
    key_info = {
        "error": "Provider connection failed",
        "status": "error",
    }

    alerts = check_key_rotation_status(key_info)

    assert len(alerts) == 1
    assert alerts[0]["name"] == "key_provider_error"
    assert alerts[0]["severity"] == "critical"
    assert "Provider connection failed" in alerts[0]["detail"]


@pytest.mark.parametrize(
    ("previous", "current", "is_downgrade"),
    [
        (3, 2, True),
        ("3", "2", True),
        (2, 3, False),
        ("v3", "v2", False),
        ("03", "2", False),
        (True, 0, False),
        (3, 2.5, False),
        (None, 2, False),
    ],
)
def test_check_key_version_downgrade_requires_canonical_integer_versions(
    previous, current, is_downgrade
):
    alerts = _check_key_version_downgrade(
        {"last_reported_version": previous}, {"current_version": current}
    )

    assert bool(alerts) is is_downgrade
    if is_downgrade:
        assert alerts == [
            {
                "name": "key_version_downgrade",
                "severity": "critical",
                "detail": "Key version downgraded from 3 to 2",
            }
        ]


def test_check_key_rotation_status_includes_downgrade_alert():
    alerts = check_key_rotation_status(
        {"current_version": 2, "hours_to_expiry": 100},
        {"last_reported_version": 3},
    )

    assert [alert["name"] for alert in alerts] == ["key_version_downgrade"]


def test_build_key_issue_body():
    """Test building the key rotation issue body."""
    timestamp = "2026-08-28T12:00:00+00:00"
    key_info = {
        "current_version": "v2",
        "state": "current",
        "provider": "test-provider",
        "rotated_at": "2026-07-28T12:00:00+00:00",
        "expires_at": "2026-09-28T12:00:00+00:00",
        "hours_to_expiry": 720.0,
        "fail_closed": True,
        "material_logged": False,
    }
    alerts = [
        {
            "name": "key_approaching_expiry",
            "severity": "warning",
            "detail": "Key version v2 expires in 720.0 hours",
        }
    ]

    body = build_key_issue_body(timestamp, key_info, alerts)

    assert "## 🔑 Key Rotation Monitoring — finance-sync" in body
    assert "**Detected at:** 2026-08-28T12:00:00+00:00" in body
    assert "| Current Version | v2 |" in body
    assert "| Key State | current |" in body
    assert "### Alerts" in body
    assert (
        "- **key_approaching_expiry** (warning): Key version v2 expires in 720.0 hours"
        in body
    )
    assert build_key_marker(datetime.fromisoformat(timestamp)) in body


def test_should_block_promotion_error():
    """Test that promotion is blocked when key provider has error."""
    key_info = {
        "error": "Provider unavailable",
        "status": "error",
    }

    assert should_block_promotion(key_info) is True


def test_should_block_promotion_expiring_soon():
    """Test that promotion is blocked when key expires soon."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 0.5,  # Less than 1 hour
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": False,
    }

    assert should_block_promotion(key_info) is True


def test_should_block_promotion_material_logged():
    """Test that promotion is blocked when material is logged."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 100,  # Far from expiry
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": True,  # Security violation
    }

    assert should_block_promotion(key_info) is True


def test_should_block_promotion_safe():
    """Test that promotion is allowed when key status is safe."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 100,  # Far from expiry
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": False,
    }

    assert should_block_promotion(key_info) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
