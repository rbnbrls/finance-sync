"""Service for reporting encryption key status without exposing key material."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync.services.key_provider import ManagedKeyProvider


class KeyStatusService:
    """Service to get key status information without exposing key material."""

    def __init__(
        self,
        key_provider: ManagedKeyProvider,
        metadata_file: str | None = None,
    ) -> None:
        """Initialize the key status service.

        Args:
            key_provider: The managed key provider to get key version and state.
            metadata_file: Path to a JSON file containing key metadata
                (rotation date and expiry) for each version.
                If not provided, will look for KEY_STATUS_METADATA_FILE
                environment variable, then default to
                "config/key-metadata.json".
        """
        self.key_provider = key_provider
        if metadata_file is None:
            metadata_file = os.environ.get(
                "KEY_STATUS_METADATA_FILE", "config/key-metadata.json"
            )
        self.metadata_file = metadata_file
        self._metadata_cache: dict[str, dict[str, str]] = {}
        self._load_metadata()

    def _load_metadata(self) -> None:
        """Load key metadata from the JSON file."""
        try:
            with open(self.metadata_file) as f:
                self._metadata_cache = json.load(f)
        except FileNotFoundError:
            # If the file doesn't exist, we'll have no metadata
            self._metadata_cache = {}
        except json.JSONDecodeError:
            # If the file is invalid, we'll have no metadata
            self._metadata_cache = {}
            # In a real implementation, we might want to log this
            # but for now we'll just note it in the cache as empty

    def get_key_status(self) -> dict[str, Any]:
        """Get the current key status without exposing key material.

        Returns:
            A dictionary containing:
                - current_version: str
                - state: str (from key provider)
                - rotation_date: str (ISO format) or None if not available
                - expires_at: str (ISO format) or None if not available
                - hours_to_expiry: float or None if not available
                - material_exposed: bool (always False for this service)
        """
        # Get the current version and state from the key provider
        # Note: We cannot get the key material from the provider without
        # exposing it, so we use the rotation_status method which only
        # returns version and state.
        provider_status = self.key_provider.rotation_status()
        current_version = provider_status.get("current_version")
        state = provider_status.get("state", "unknown")

        # Get metadata for this version
        metadata = self._metadata_cache.get(current_version, {})

        rotation_date_str = metadata.get("rotation_date")
        expires_at_str = metadata.get("expires_at")

        # Calculate hours to expiry if we have an expiry date
        hours_to_expiry = None
        if expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str)
                now = datetime.now(UTC)
                if expires_at > now:
                    hours_to_expiry = (expires_at - now).total_seconds() / 3600
                else:
                    hours_to_expiry = 0.0  # Already expired
            except ValueError:
                # Invalid date format
                hours_to_expiry = None

        return {
            "current_version": current_version,
            "state": state,
            "rotation_date": rotation_date_str,
            "expires_at": expires_at_str,
            "hours_to_expiry": hours_to_expiry,
            "material_exposed": False,  # By design of this service
        }

    def is_approaching_expiry(self, threshold_hours: int = 24) -> bool:
        """Check if the current key is approaching expiry within the threshold.

        Args:
            threshold_hours: Number of hours before expiry to consider
                as approaching.

        Returns:
            True if the key is approaching expiry, False otherwise.
            Returns False if expiry information is not available.
        """
        status = self.get_key_status()
        hours_to_expiry = status.get("hours_to_expiry")
        if hours_to_expiry is None:
            return False
        return hours_to_expiry <= threshold_hours

    def is_expired(self) -> bool:
        """Check if the current key is expired.

        Returns:
            True if the key is expired, False otherwise.
            Returns False if expiry information is not available.
        """
        status = self.get_key_status()
        hours_to_expiry = status.get("hours_to_expiry")
        if hours_to_expiry is None:
            return False
        return hours_to_expiry <= 0


def get_key_status_from_env() -> dict[str, Any]:
    """Convenience function to get key status from environment-configured
    provider.

    This function expects the following environment variables:
        - KEY_STATUS_CURRENT_VERSION: The current key version
        - KEY_STATUS_ROTATION_DATE: ISO format rotation date for the
          current version
        - KEY_STATUS_EXPIRES_AT: ISO format expiry date for the current version
        - KEY_STATUS_STATE: The key state (optional, defaults to "current")

    Returns:
        A dictionary with the key status information.

    Note: This function is intended for use in environments where a full
    ManagedKeyProvider cannot be configured (e.g., in simple monitoring
    scripts).
    """
    current_version = os.environ.get("KEY_STATUS_CURRENT_VERSION")
    if not current_version:
        return {
            "current_version": None,
            "state": None,
            "rotation_date": None,
            "expires_at": None,
            "hours_to_expiry": None,
            "material_exposed": False,
        }

    rotation_date_str = os.environ.get("KEY_STATUS_ROTATION_DATE")
    expires_at_str = os.environ.get("KEY_STATUS_EXPIRES_AT")
    state = os.environ.get("KEY_STATUS_STATE", "current")

    hours_to_expiry = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now(UTC)
            if expires_at > now:
                hours_to_expiry = (expires_at - now).total_seconds() / 3600
            else:
                hours_to_expiry = 0.0
        except ValueError:
            hours_to_expiry = None

    return {
        "current_version": current_version,
        "state": state,
        "rotation_date": rotation_date_str,
        "expires_at": expires_at_str,
        "hours_to_expiry": hours_to_expiry,
        "material_exposed": False,
    }
