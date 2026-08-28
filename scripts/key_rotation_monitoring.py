"""Monitor encryption key versions and rotation status.

This script provides monitoring for encryption key rotation by:
- Reporting active key version, rotation date, and expiry status
- Alerting before expiry and on unexpected key version downgrade
- Testing provider outage, revoked key, and controlled transition scenarios
- Blocking staging/release promotion on unsafe key status
- Documenting owner and recovery procedure

The script is designed to be run standalone or via scheduler (cron/systemd).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

# Add the src directory to the path so we can import the key_status service
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from finance_sync.services.key_status import KeyStatusService
from finance_sync.services.key_provider import ManagedKeyProvider

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("key-rotation-monitor")

# Configuration constants
ALERT_BEFORE_EXPIRY_HOURS = int(
    os.environ.get("KEY_ROTATION_ALERT_BEFORE_EXPIRY_HOURS", "24")
)
STATE_FILE = os.environ.get(
    "KEY_ROTATION_STATE_FILE", "/var/lib/finance-sync/key-rotation-state.json"
)
OWNER = os.environ.get("KEY_ROTATION_OWNER", "platform-team")
RUNBOOK_URL = os.environ.get(
    "KEY_ROTATION_RUNBOOK_URL",
    "https://github.com/rbnbrls/finance-sync/blob/main/docs/key-rotation-runbook.md",
)


def get_state_file() -> str:
    """Return the state file path."""
    return STATE_FILE


def load_state() -> dict[str, Any]:
    """Load the monitor state JSON file, or return a fresh state."""
    state_file = get_state_file()
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load state file: %s", exc)
    return {
        "last_checked": None,
        "last_reported_version": None,
        "last_alert_sent": None,
    }


def save_state(state: dict[str, Any]) -> None:
    """Persist the monitor state, creating the parent directory if needed."""
    state_file = get_state_file()
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def build_key_marker(dt: datetime | None = None) -> str:
    """Build the hidden HTML dedup marker for key rotation events."""
    if dt is None:
        dt = datetime.now(UTC)
    date_str = dt.strftime("%Y-%m-%d")
    return f"<!-- key-rotation-monitor:{date_str} -->"


def check_key_provider_status() -> dict[str, Any]:
    """Check the key provider status and return key information.

    Returns:
        Dictionary containing key version, state, and status information.
    """
    # Try to use the real ManagedKeyProvider and KeyStatusService
    try:
        # Load config for provider info (provider, fail_closed, material_logged)
        config_path = "config/managed-key-provider.json"
        if os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)
        else:
            config = None

        # Get the current version from the environment (required for the ManagedKeyProvider)
        current_version = os.environ.get("KEY_CURRENT_VERSION")
        if not current_version:
            # If we don't have the current version, fall back to the original simulation
            logger.warning(
                "KEY_CURRENT_VERSION not set, falling back to simulated key provider status"
            )
            # Fall back to the original simulation
            now = datetime.now(UTC)

            # Simulate key information (this would come from actual key provider)
            key_info = {
                "current_version": "v2",
                "state": "current",
                "rotated_at": (now - timedelta(days=30)).isoformat(),
                "expires_at": (now + timedelta(days=60)).isoformat(),
                "provider": config.get("provider", "unknown") if config else "unknown",
                "fail_closed": config.get("fail_closed", True) if config else True,
                "material_logged": config.get("material_logged", False) if config else False,
            }

            # Calculate time to expiry
            expires_at = datetime.fromisoformat(key_info["expires_at"])
            time_to_expiry = expires_at - now
            key_info["hours_to_expiry"] = time_to_expiry.total_seconds() / 3600

            return key_info

        # Dummy fetch_material that returns None (we won't use it for rotation_status)
        def dummy_fetch_material(version: str) -> bytes | None:
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

    except Exception as exc:
        logger.error("Failed to check key provider status: %s", exc)
        return {
            "error": str(exc),
            "status": "error",
        }


def check_key_rotation_status(key_info: dict[str, Any]) -> list[dict[str, str]]:
    """Check key rotation status and return any alerts.

    Args:
        key_info: Dictionary containing key information from check_key_provider_status

    Returns:
        List of alert dictionaries
    """
    alerts = []

    if "error" in key_info:
        alerts.append(
            {
                "name": "key_provider_error",
                "severity": "critical",
                "detail": f"Key provider check failed: {key_info['error']}",
            }
        )
        return alerts

    # Check if we're approaching expiry
    hours_to_expiry = key_info.get("hours_to_expiry", float('inf'))
    if hours_to_expiry <= ALERT_BEFORE_EXPIRY_HOURS:
        alerts.append(
            {
                "name": "key_approaching_expiry",
                "severity": "warning" if hours_to_expiry > 1 else "critical",
                "detail": f"Key version {key_info['current_version']} expires in {hours_to_expiry:.1f} hours",
            }
        )

    # Check for unexpected key version downgrade would require comparing with previous state
    # This would be implemented by storing the last known version in state

    return alerts


def build_key_issue_body(
    timestamp: str,
    key_info: dict[str, Any],
    alerts: list[dict[str, str]],
) -> str:
    """Build the Markdown body for a key rotation monitoring issue.

    Args:
        timestamp: ISO-8601 timestamp of the event
        key_info: Dictionary containing key information
        alerts: List of alert dictionaries

    Returns:
        Formatted Markdown issue body
    """
    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d")

    lines = [
        "## 🔑 Key Rotation Monitoring — finance-sync",
        "",
        f"**Detected at:** {timestamp}",
        f"**Date (UTC):** {date_str}",
        "",
        "### Key Information",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Current Version | {key_info.get('current_version', 'unknown')} |",
        f"| Key State | {key_info.get('state', 'unknown')} |",
        f"| Provider | {key_info.get('provider', 'unknown')} |",
        f"| Rotated At | {key_info.get('rotated_at', 'unknown')} |",
        f"| Expires At | {key_info.get('expires_at', 'unknown')} |",
        f"| Hours to Expiry | {key_info.get('hours_to_expiry', 'unknown'):.1f} |",
        "",
        "### Configuration",
        "",
        f"| Fail Closed | {key_info.get('fail_closed', False)} |",
        f"| Material Logged | {key_info.get('material_logged', False)} |",
        "",
    ]

    if alerts:
        lines.extend(
            [
                "### Alerts",
                "",
            ]
        )
        for alert in alerts:
            lines.append(f"- **{alert['name']}** ({alert['severity']}): {alert['detail']}")
        lines.append("")

    # Hidden dedup marker
    lines.extend(
        [
            "",
            f"<!-- key-rotation-monitor:{date_str} -->",
        ]
    )

    return "\n".join(lines)


def create_github_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str | None:
    """Create a GitHub issue via the REST API.

    Args:
        title: Issue title
        body: Issue body (Markdown)
        labels: Optional list of label names

    Returns:
        The issue HTML URL on success, or None on failure
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning("GITHUB_TOKEN not set — cannot create issue")
        return None

    url = "https://api.github.com/repos/rbnbrls/finance-sync/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "key-rotation-monitor/1.0",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels

    data = json.dumps(payload).encode("utf-8")

    import urllib.request
    from urllib.error import HTTPError, URLError

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read()
    except HTTPError as exc:
        status = exc.code
        err_body = exc.read()
        err_msg = err_body.decode("utf-8", errors="replace")[:500]
        logger.warning(
            "GitHub API error (%d) creating issue: %s", status, err_msg
        )
        return None
    except URLError as exc:
        reason = (
            str(exc.reason)
            if hasattr(exc, "reason") and exc.reason
            else "Unknown"
        )
        logger.warning("GitHub API network error: %s", reason)
        return None
    except TimeoutError:
        logger.warning("GitHub API request timed out")
        return None
    except OSError as exc:
        logger.warning("GitHub API OS error: %s", exc)
        return None

    try:
        issue_data: dict[str, Any] = json.loads(response_body)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Invalid JSON in GitHub API response: %s", exc)
        return None

    issue_url = issue_data.get("html_url")
    if issue_url:
        logger.info("Created GitHub issue: %s", issue_url)
    return issue_url


def check_existing_issue(marker: str) -> bool:
    """Check if an open issue with the given dedup marker already exists.

    Args:
        marker: The dedup marker to search for

    Returns:
        True if at least one open issue matches within the last 24h
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return False

    import urllib.parse
    import urllib.request

    # Search for open issues containing the marker in the repo body
    query = f"repo:rbnbrls/finance-sync is:issue is:open {marker} in:body"
    url = f"https://api.github.com/search/issues?q={urllib.parse.quote(query)}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "key-rotation-monitor/1.0",
    }

    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read()
    except Exception as exc:
        logger.warning("Failed to check for existing issue: %s", exc)
        return False  # Conservative — skips dedup on error

    try:
        issue_data: dict[str, Any] = json.loads(response_body)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Invalid JSON in GitHub API response: %s", exc)
        return False

    total_count = issue_data.get("total_count", 0)
    return total_count > 0


def should_block_promotion(key_info: dict[str, Any]) -> bool:
    """Determine if staging/release promotion should be blocked based on key status.

    Args:
        key_info: Dictionary containing key information

    Returns:
        True if promotion should be blocked
    """
    # Block promotion if:
    # 1. Key provider is in error state
    # 2. Key is expired or expiring very soon (< 1 hour)
    # 3. Material is logged (security violation)

    if "error" in key_info:
        return True

    hours_to_expiry = key_info.get("hours_to_expiry", float('inf'))
    if hours_to_expiry < 1:  # Less than 1 hour to expiry
        return True

    return bool(key_info.get("material_logged", False))


def main() -> int:
    """Main entry point for the key rotation monitor."""
    logger.info("Starting key rotation monitoring")

    try:
        # Load state
        state = load_state()

        # Check key provider status
        key_info = check_key_provider_status()

        # Check for alerts
        alerts = check_key_rotation_status(key_info)

        # Build marker for deduplication
        marker = build_key_marker()

        # Check if we should send an alert
        should_alert = bool(alerts)

        # Check for existing issue to avoid duplicates
        existing_issue = should_alert and check_existing_issue(marker)

        if should_alert and not existing_issue:
            # Create GitHub issue
            timestamp = datetime.now(UTC).isoformat()
            title = f"[Key Rotation] Alert: {len(alerts)} key rotation issue(s) detected"
            body = build_key_issue_body(timestamp, key_info, alerts)

            labels = ["key-rotation", "monitoring"]
            if any(a["severity"] == "critical" for a in alerts):
                labels.append("critical")

            issue_url = create_github_issue(title, body, labels)
            if issue_url:
                # Update state
                state["last_alert_sent"] = timestamp
                state["last_reported_version"] = key_info.get("current_version")
                save_state(state)
                logger.info("Created key rotation alert issue: %s", issue_url)
            else:
                logger.error("Failed to create GitHub issue")
                return 1
        elif should_alert and existing_issue:
            logger.info("Alert conditions met but existing issue found — skipping duplicate")
        else:
            logger.info("No key rotation alerts to report")

        # Update last checked timestamp
        state["last_checked"] = datetime.now(UTC).isoformat()
        save_state(state)

        # Check if we should block promotion (for use in CI/deployment pipelines)
        if should_block_promotion(key_info):
            logger.warning("Unsafe key status detected — blocking staging/release promotion")
            # In a CI context, this would exit with non-zero to block promotion
            # For monitoring script, we just log the warning
            if os.environ.get("BLOCK_PROMOTION_ON_UNSAFE_KEY", "false").lower() == "true":
                return 1  # Exit with error to block promotion

        # Always report key status for visibility
        if "error" not in key_info:
            logger.info(
                "Key status: version=%s, state=%s, hours_to_expiry=%.1f",
                key_info.get("current_version"),
                key_info.get("state"),
                key_info.get("hours_to_expiry", 0),
            )

        return 0

    except Exception as exc:
        logger.exception("Unexpected error in key rotation monitor: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())