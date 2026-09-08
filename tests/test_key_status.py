"""Tests for the key status service."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from finance_sync.services.key_provider import ManagedKeyProvider
from finance_sync.services.key_status import (
    KeyStatusService,
    get_key_status_from_env,
)


def test_key_status_service_initialization() -> None:
    """Test that the service initializes correctly."""
    # Create a mock key provider
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    # Create a temporary metadata file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(
            {
                "v2": {
                    "rotation_date": "2026-08-01T00:00:00+00:00",
                    "expires_at": "2026-09-01T00:00:00+00:00",
                }
            },
            f,
        )
        metadata_file = f.name

    try:
        service = KeyStatusService(
            key_provider=mock_provider, metadata_file=metadata_file
        )
        assert service.key_provider == mock_provider
        assert service.metadata_file == metadata_file
        assert service._metadata_cache == {
            "v2": {
                "rotation_date": "2026-08-01T00:00:00+00:00",
                "expires_at": "2026-09-01T00:00:00+00:00",
            }
        }
    finally:
        os.unlink(metadata_file)


def test_key_status_service_loads_metadata() -> None:
    """Test that the service loads metadata from the file."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    metadata = {
        "v2": {
            "rotation_date": "2026-08-01T00:00:00+00:00",
            "expires_at": "2026-09-01T00:00:00+00:00",
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(metadata, f)
        metadata_file = f.name

    try:
        service = KeyStatusService(
            key_provider=mock_provider, metadata_file=metadata_file
        )
        assert service._metadata_cache == metadata
    finally:
        os.unlink(metadata_file)


def test_key_status_service_handles_missing_metadata_file() -> None:
    """Test that the service handles a missing metadata file gracefully."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    service = KeyStatusService(
        key_provider=mock_provider, metadata_file="/non/existent/file.json"
    )
    assert service._metadata_cache == {}


def test_key_status_service_handles_invalid_metadata_file() -> None:
    """Test that the service handles an invalid metadata file gracefully."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        f.write("invalid json")
        metadata_file = f.name

    try:
        service = KeyStatusService(
            key_provider=mock_provider, metadata_file=metadata_file
        )
        assert service._metadata_cache == {}
    finally:
        os.unlink(metadata_file)


def test_key_status_service_get_key_status() -> None:
    """Test that the service returns the correct key status."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    # Set the current time to a known value for consistent testing
    fixed_time = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)

    metadata = {
        "v2": {
            "rotation_date": "2026-08-01T00:00:00+00:00",
            "expires_at": "2026-09-01T00:00:00+00:00",
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(metadata, f)
        metadata_file = f.name

    try:
        with patch(
            "finance_sync.services.key_status.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_time
            mock_datetime.fromisoformat = datetime.fromisoformat

            service = KeyStatusService(
                key_provider=mock_provider, metadata_file=metadata_file
            )
            status = service.get_key_status()

            assert (
                status
                == {
                    "current_version": "v2",
                    "state": "current",
                    "rotation_date": "2026-08-01T00:00:00+00:00",
                    "expires_at": "2026-09-01T00:00:00+00:00",
                    "hours_to_expiry": 396.0,  # From 2026-08-15 12:00 to 2026-09-01 00:00 is 396 hours
                    "material_exposed": False,
                }
            )
    finally:
        os.unlink(metadata_file)


def test_key_status_service_get_key_status_expired() -> None:
    """Test that the service returns correct status when the key is expired."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    fixed_time = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)

    metadata = {
        "v2": {
            "rotation_date": "2026-07-01T00:00:00+00:00",
            "expires_at": "2026-07-31T00:00:00+00:00",  # Already expired
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(metadata, f)
        metadata_file = f.name

    try:
        with patch(
            "finance_sync.services.key_status.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_time
            mock_datetime.fromisoformat = datetime.fromisoformat

            service = KeyStatusService(
                key_provider=mock_provider, metadata_file=metadata_file
            )
            status = service.get_key_status()

            assert status["hours_to_expiry"] == 0.0  # Expired
            assert status["material_exposed"] is False
    finally:
        os.unlink(metadata_file)


def test_key_status_service_is_approaching_expiry() -> None:
    """Test the is_approaching_expiry method."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    fixed_time = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)

    # Set expiry to 20 hours from now (within 24 hour threshold)
    expiry_time = fixed_time + timedelta(hours=20)

    metadata = {
        "v2": {
            "rotation_date": "2026-08-01T00:00:00+00:00",
            "expires_at": expiry_time.isoformat(),
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(metadata, f)
        metadata_file = f.name

    try:
        with patch(
            "finance_sync.services.key_status.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_time
            mock_datetime.fromisoformat = datetime.fromisoformat

            service = KeyStatusService(
                key_provider=mock_provider, metadata_file=metadata_file
            )

            # Should be approaching expiry (20 hours < 24 hours)
            assert service.is_approaching_expiry(threshold_hours=24) is True
            # Should not be approaching expiry with a 10 hour threshold
            assert service.is_approaching_expiry(threshold_hours=10) is False
    finally:
        os.unlink(metadata_file)


def test_key_status_service_is_expired() -> None:
    """Test the is_expired method."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    fixed_time = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)

    # Test with expired key
    metadata_expired = {
        "v2": {
            "rotation_date": "2026-07-01T00:00:00+00:00",
            "expires_at": "2026-07-31T00:00:00+00:00",  # Expired
        }
    }

    # Test with valid key
    metadata_valid = {
        "v2": {
            "rotation_date": "2026-08-01T00:00:00+00:00",
            "expires_at": "2026-09-01T00:00:00+00:00",  # Valid
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f_expired:
        json.dump(metadata_expired, f_expired)
        metadata_file_expired = f_expired.name

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f_valid:
        json.dump(metadata_valid, f_valid)
        metadata_file_valid = f_valid.name

    try:
        with patch(
            "finance_sync.services.key_status.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_time
            mock_datetime.fromisoformat = datetime.fromisoformat

            # Test expired key
            service_expired = KeyStatusService(
                key_provider=mock_provider, metadata_file=metadata_file_expired
            )
            assert service_expired.is_expired() is True

            # Test valid key
            service_valid = KeyStatusService(
                key_provider=mock_provider, metadata_file=metadata_file_valid
            )
            assert service_valid.is_expired() is False
    finally:
        os.unlink(metadata_file_expired)
        os.unlink(metadata_file_valid)


def test_key_status_service_no_key_material_exposed() -> None:
    """Test that the service never exposes key material."""
    mock_provider = Mock(spec=ManagedKeyProvider)
    mock_provider.rotation_status.return_value = {
        "current_version": "v2",
        "state": "current",
    }

    metadata = {
        "v2": {
            "rotation_date": "2026-08-01T00:00:00+00:00",
            "expires_at": "2026-09-01T00:00:00+00:00",
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(metadata, f)
        metadata_file = f.name

    try:
        service = KeyStatusService(
            key_provider=mock_provider, metadata_file=metadata_file
        )
        status = service.get_key_status()

        # Ensure that no key material is present in the status
        assert "material" not in status
        assert status["material_exposed"] is False

        # Also check that the service does not have access to the key material
        # through the key provider (the provider's rotation_status doesn't return material)
        mock_provider.rotation_status.assert_called_once()
        mock_provider.current.assert_not_called()  # We never call current() which would return material
        mock_provider.fetch.assert_not_called()  # We never call fetch() which would return material
    finally:
        os.unlink(metadata_file)


def test_get_key_status_from_env() -> None:
    """Test the get_key_status_from_env function."""
    # Test with all environment variables set
    with patch.dict(
        os.environ,
        {
            "KEY_STATUS_CURRENT_VERSION": "v2",
            "KEY_STATUS_ROTATION_DATE": "2026-08-01T00:00:00+00:00",
            "KEY_STATUS_EXPIRES_AT": "2026-09-01T00:00:00+00:00",
            "KEY_STATUS_STATE": "current",
        },
    ):
        fixed_time = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
        with patch(
            "finance_sync.services.key_status.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_time
            mock_datetime.fromisoformat = datetime.fromisoformat

            status = get_key_status_from_env()

            assert (
                status
                == {
                    "current_version": "v2",
                    "state": "current",
                    "rotation_date": "2026-08-01T00:00:00+00:00",
                    "expires_at": "2026-09-01T00:00:00+00:00",
                    "hours_to_expiry": 396.0,  # From 2026-08-15 12:00 to 2026-09-01 00:00 is 396 hours
                    "material_exposed": False,
                }
            )

    # Test with missing required variables
    with patch.dict(os.environ, {}, clear=True):
        status = get_key_status_from_env()
        assert status == {
            "current_version": None,
            "state": None,
            "rotation_date": None,
            "expires_at": None,
            "hours_to_expiry": None,
            "material_exposed": False,
        }

    # Test with missing expiry (should still work but hours_to_expiry will be None)
    with patch.dict(
        os.environ,
        {
            "KEY_STATUS_CURRENT_VERSION": "v2",
            "KEY_STATUS_ROTATION_DATE": "2026-08-01T00:00:00+00:00",
            # KEY_STATUS_EXPIRES_AT not set
            "KEY_STATUS_STATE": "current",
        },
    ):
        status = get_key_status_from_env()
        assert status == {
            "current_version": "v2",
            "state": "current",
            "rotation_date": "2026-08-01T00:00:00+00:00",
            "expires_at": None,
            "hours_to_expiry": None,
            "material_exposed": False,
        }

    # Test with invalid expiry date
    with patch.dict(
        os.environ,
        {
            "KEY_STATUS_CURRENT_VERSION": "v2",
            "KEY_STATUS_ROTATION_DATE": "2026-08-01T00:00:00+00:00",
            "KEY_STATUS_EXPIRES_AT": "invalid-date",
            "KEY_STATUS_STATE": "current",
        },
    ):
        status = get_key_status_from_env()
        assert status["expires_at"] == "invalid-date"
        assert status["hours_to_expiry"] is None
