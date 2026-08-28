"""Tests for the key status promotion gate."""

from __future__ import annotations

from unittest.mock import patch

from scripts.check_key_status_for_promotion import (
    check_key_provider_status_with_env_vars,
    evaluate_key_status_for_promotion,
    main,
)


def test_check_key_provider_status_with_env_vars():
    """Test key provider status check by verifying it returns expected structure."""
    # Test with simulated data (no KEY_CURRENT_VERSION)
    env_vars = {}
    result = check_key_provider_status_with_env_vars(env_vars)

    # Should return a dict with key information
    assert isinstance(result, dict)
    # Should have error since no KEY_CURRENT_VERSION
    assert "error" in result
    assert (
        result["error"]
        == "KEY_CURRENT_VERSION not found in staging app environment variables"
    )
    assert result["status"] == "error"


def test_check_key_provider_status_with_env_vars_has_version():
    """Test key provider status check when KEY_CURRENT_VERSION is present."""
    env_vars = {
        "KEY_CURRENT_VERSION": "v2",
        # Other vars will use defaults from the service
    }
    result = check_key_provider_status_with_env_vars(env_vars)

    # Should return a dict with key information
    assert isinstance(result, dict)
    # Should not have error
    assert "error" not in result
    assert result["current_version"] == "v2"
    assert "state" in result
    assert "hours_to_expiry" in result


def test_evaluate_key_status_for_promotion_error():
    """Test that evaluation blocks promotion when key provider has error."""
    key_info = {
        "error": "Provider unavailable",
        "status": "error",
    }

    is_safe, reason = evaluate_key_status_for_promotion(key_info)

    assert is_safe is False
    assert "Key provider error: Provider unavailable" in reason


def test_evaluate_key_status_for_promotion_expiring_soon():
    """Test that evaluation blocks promotion when key expires soon."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 0.5,  # Less than 1 hour
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": False,
    }

    is_safe, reason = evaluate_key_status_for_promotion(key_info)

    assert is_safe is False
    assert "Key expires in 0.5 hours (less than 1 hour)" in reason


def test_evaluate_key_status_for_promotion_material_logged():
    """Test that evaluation blocks promotion when material is logged."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 100,  # Far from expiry
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": True,  # Security violation
    }

    is_safe, reason = evaluate_key_status_for_promotion(key_info)

    assert is_safe is False
    assert "Key material has been logged (security violation)" in reason


def test_evaluate_key_status_for_promotion_safe():
    """Test that evaluation allows promotion when key status is safe."""
    key_info = {
        "current_version": "v2",
        "state": "current",
        "hours_to_expiry": 100,  # Far from expiry
        "provider": "test-provider",
        "fail_closed": True,
        "material_logged": False,
    }

    is_safe, reason = evaluate_key_status_for_promotion(key_info)

    assert is_safe is True
    assert reason == "Key status is safe for promotion"


def test_main_blocks_on_error(capsys):
    """Test that main blocks promotion when key provider has error."""
    with (
        patch(
            "scripts.check_key_status_for_promotion.get_staging_app_env_vars"
        ) as mock_get_env,
        patch(
            "scripts.check_key_status_for_promotion.check_key_provider_status_with_env_vars"
        ) as mock_check,
        patch.dict(
            "os.environ",
            {
                "COOLIFY_BASE_URL": "https://dev.7rb.nl",
                "COOLIFY_TOKEN": "test-token",
                "STAGING_APP_UUID": "test-uuid",
            },
        ),
    ):
        mock_get_env.return_value = {
            "KEY_CURRENT_VERSION": "v2"
        }  # To pass the env var check
        mock_check.return_value = {
            "error": "Provider unavailable",
            "status": "error",
        }

        # Call main
        exit_code = main()

        # Capture stdout and stderr
        captured = capsys.readouterr()

        # Check that it returned error code
        assert exit_code == 1

        # Check that error message was printed
        assert "❌ Key provider error: Provider unavailable" in captured.out


def test_main_blocks_on_expiring_soon(capsys):
    """Test that main blocks promotion when key expires soon."""
    with (
        patch(
            "scripts.check_key_status_for_promotion.get_staging_app_env_vars"
        ) as mock_get_env,
        patch(
            "scripts.check_key_status_for_promotion.check_key_provider_status_with_env_vars"
        ) as mock_check,
        patch.dict(
            "os.environ",
            {
                "COOLIFY_BASE_URL": "https://dev.7rb.nl",
                "COOLIFY_TOKEN": "test-token",
                "STAGING_APP_UUID": "test-uuid",
            },
        ),
    ):
        mock_get_env.return_value = {"KEY_CURRENT_VERSION": "v2"}
        mock_check.return_value = {
            "current_version": "v2",
            "state": "current",
            "hours_to_expiry": 0.5,  # Less than 1 hour
            "provider": "test-provider",
            "fail_closed": True,
            "material_logged": False,
        }

        # Call main
        exit_code = main()

        # Capture stdout and stderr
        captured = capsys.readouterr()

        # Check that it returned error code
        assert exit_code == 1

        # Check that error message was printed
        assert "❌ Key expires in 0.5 hours (less than 1 hour)" in captured.out


def test_main_blocks_on_material_logged(capsys):
    """Test that main blocks promotion when material is logged."""
    with (
        patch(
            "scripts.check_key_status_for_promotion.get_staging_app_env_vars"
        ) as mock_get_env,
        patch(
            "scripts.check_key_status_for_promotion.check_key_provider_status_with_env_vars"
        ) as mock_check,
        patch.dict(
            "os.environ",
            {
                "COOLIFY_BASE_URL": "https://dev.7rb.nl",
                "COOLIFY_TOKEN": "test-token",
                "STAGING_APP_UUID": "test-uuid",
            },
        ),
    ):
        mock_get_env.return_value = {"KEY_CURRENT_VERSION": "v2"}
        mock_check.return_value = {
            "current_version": "v2",
            "state": "current",
            "hours_to_expiry": 100,  # Far from expiry
            "provider": "test-provider",
            "fail_closed": True,
            "material_logged": True,  # Security violation
        }

        # Call main
        exit_code = main()

        # Capture stdout and stderr
        captured = capsys.readouterr()

        # Check that it returned error code
        assert exit_code == 1

        # Check that error message was printed
        assert (
            "❌ Key material has been logged (security violation)"
            in captured.out
        )


def test_main_allows_safe_key(capsys):
    """Test that main allows promotion when key status is safe."""
    with (
        patch(
            "scripts.check_key_status_for_promotion.get_staging_app_env_vars"
        ) as mock_get_env,
        patch(
            "scripts.check_key_status_for_promotion.check_key_provider_status_with_env_vars"
        ) as mock_check,
        patch.dict(
            "os.environ",
            {
                "COOLIFY_BASE_URL": "https://dev.7rb.nl",
                "COOLIFY_TOKEN": "test-token",
                "STAGING_APP_UUID": "test-uuid",
            },
        ),
    ):
        mock_get_env.return_value = {"KEY_CURRENT_VERSION": "v2"}
        mock_check.return_value = {
            "current_version": "v2",
            "state": "current",
            "hours_to_expiry": 100,  # Far from expiry
            "provider": "test-provider",
            "fail_closed": True,
            "material_logged": False,
        }

        # Call main
        exit_code = main()

        # Capture stdout and stderr
        captured = capsys.readouterr()

        # Check that it returned success code
        assert exit_code == 0

        # Check that success message was printed
        assert "✅ Key status is safe for promotion" in captured.out
