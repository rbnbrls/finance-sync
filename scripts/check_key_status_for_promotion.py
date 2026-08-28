#!/usr/bin/env python3
"""
Check if the key status is safe for promotion using the Coolify API to get the current key version.

This script is used as a gate in the release workflow to block staging/release promotion
whenever the managed key status is unsafe.
"""

import json
import os
import sys
from datetime import UTC, datetime
from typing import Dict, Optional, Tuple

# Add the src directory to the path so we can import the key_status service
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from finance_sync.services.key_status import KeyStatusService
from finance_sync.services.key_provider import ManagedKeyProvider
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def get_staging_app_env_vars(coolify_base_url: str, coolify_token: str, staging_app_uuid: str) -> Dict[str, str]:
    """Fetch the environment variables for the staging app from the Coolify API.

    Args:
        coolify_base_url: The base URL of the Coolify instance (e.g., https://dev.7rb.nl).
        coolify_token: The Coolify API token.
        staging_app_uuid: The UUID of the staging app in Coolify.

    Returns:
        A dictionary of environment variable names to values.
    """
    url = f"{coolify_base_url}/api/v1/applications/{staging_app_uuid}/env"
    headers = {
        "Authorization": f"Bearer {coolify_token}",
        "Accept": "application/json",
        "User-Agent": "key-status-check/1.0",
    }
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=10) as response:
            if response.status == 200:
                data = json.loads(response.read().decode())
                # The API returns a list of objects with "name" and "value"
                env_vars = {}
                for item in data:
                    env_vars[item["name"]] = item["value"]
                return env_vars
            else:
                print(f"❌ Failed to fetch env vars: HTTP {response.status}")
                return {}
    except (HTTPError, URLError) as exc:
        print(f"❌ Error fetching env vars: {exc}")
        return {}


def check_key_provider_status_with_env_vars(env_vars: Dict[str, str]) -> Dict[str, any]:
    """Check the key provider status using the provided environment variables.

    This function mimics the logic in scripts/key_rotation_monitoring.py:check_key_provider_status
    but uses the provided environment variables instead of reading from os.environ directly.

    Args:
        env_vars: Dictionary of environment variables.

    Returns:
        Dictionary containing key version, state, and status information.
    """
    # Get the current version from the environment variables
    current_version = env_vars.get("KEY_CURRENT_VERSION")
    if not current_version:
        # If we don't have the current version, we cannot check the real status.
        # We'll return an error.
        return {
            "error": "KEY_CURRENT_VERSION not found in staging app environment variables",
            "status": "error",
        }

    # Load config for provider info (provider, fail_closed, material_logged)
    config_path = "config/managed-key-provider.json"
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = None

    # Dummy fetch_material that returns None (we won't use it for rotation_status)
    def dummy_fetch_material(version: str):
        return None

    # Initialize the ManagedKeyProvider with the current version and dummy fetch_material
    provider = ManagedKeyProvider(
        current_version=current_version,
        fetch_material=dummy_fetch_material,
        revoked_versions=frozenset(),
    )

    # Get the rotation status from the provider (which gives us current_version and state)
    provider_status = provider.rotation_status()
    # provider_status returns {"current_version": ..., "state": "managed"}

    # Use the KeyStatusService to get the full status (including rotation date and expiry)
    service = KeyStatusService(key_provider=provider)
    key_status = service.get_key_status()

    # Build the result dictionary to match the original simulation's keys
    result = {
        "current_version": key_status.get("current_version"),
        "state": key_status.get("state"),  # This is from the provider's rotation_status, which is "managed"
        "rotated_at": None,  # We don't have this information without fetching the key material
        "expires_at": key_status.get("expires_at"),
        "provider": config.get("provider", "unknown") if config else "unknown",
        "fail_closed": config.get("fail_closed", True) if config else True,
        "material_logged": config.get("material_logged", False) if config else False,
        "hours_to_expiry": key_status.get("hours_to_expiry"),
    }

    return result


def evaluate_key_status_for_promotion(key_info: Dict[str, any]) -> Tuple[bool, str]:
    """Evaluate if the key status is safe for promotion.

    Args:
        key_info: Dictionary containing key information from check_key_provider_status_with_env_vars

    Returns:
        Tuple of (is_safe, reason) where is_safe is True if promotion should be allowed,
        and reason explains the decision.
    """
    # Block promotion if:
    # 1. Key provider is in error state
    # 2. Key is expired or expiring very soon (< 1 hour)
    # 3. Material is logged (security violation)

    if "error" in key_info:
        return False, f"Key provider error: {key_info['error']}"

    hours_to_expiry = key_info.get("hours_to_expiry", float('inf'))
    if hours_to_expiry < 1:  # Less than 1 hour to expiry
        return False, f"Key expires in {hours_to_expiry:.1f} hours (less than 1 hour)"

    if key_info.get("material_logged", False):
        return False, "Key material has been logged (security violation)"

    # If we get here, the key status is safe
    return True, "Key status is safe for promotion"


def main() -> int:
    """Main entry point for the key status check."""
    # Configure logging
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    global logger
    logger = logging.getLogger("key-status-check")

    logger.info("Checking key status for promotion gate")

    try:
        # Get the Coolify base URL and token from the environment
        coolify_base_url = os.environ.get("COOLIFY_BASE_URL")
        coolify_token = os.environ.get("COOLIFY_TOKEN")
        staging_app_uuid = os.environ.get("STAGING_APP_UUID")

        if not all([coolify_base_url, coolify_token, staging_app_uuid]):
            missing = []
            if not coolify_base_url:
                missing.append("COOLIFY_BASE_URL")
            if not coolify_token:
                missing.append("COOLIFY_TOKEN")
            if not staging_app_uuid:
                missing.append("STAGING_APP_UUID")
            logger.error(f"Missing environment variables: {', '.join(missing)}")
            print(f"❌ Missing environment variables: {', '.join(missing)}")
            return 1

        # Fetch the staging app's environment variables
        env_vars = get_staging_app_env_vars(coolify_base_url, coolify_token, staging_app_uuid)
        if not env_vars:
            logger.error("Failed to fetch environment variables from the staging app")
            print("❌ Failed to fetch environment variables from the staging app")
            return 1

        # Check key provider status using the fetched environment variables
        key_info = check_key_provider_status_with_env_vars(env_vars)

        # Evaluate if promotion should be allowed
        is_safe, reason = evaluate_key_status_for_promotion(key_info)
        
        if is_safe:
            logger.info(reason)
            print(f"✅ {reason}")
            return 0
        else:
            logger.error(reason)
            print(f"❌ {reason}")
            return 1

    except Exception as exc:
        logger.error(f"Unexpected error: {exc}")
        print(f"❌ Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())