"""Standalone health monitor for the finance-sync deployment.

Checks the app/worker health endpoints, polls the Coolify API for the
application status and restart count, samples container CPU/memory via
``docker stats``, and files GitHub issues on crashes and resource
threshold alerts (with daily dedup markers).

Fully decoupled from Hermes: all configuration comes from the
environment (``COOLIFY_API_TOKEN``, ``GITHUB_TOKEN``, ``STATE_FILE``).
Schedule it standalone with the systemd units in ``deploy/systemd/``
(or any other scheduler) — nothing finance-sync related runs via Hermes
cron.

Invocation::

    finance-sync-monitor                     # console script
    python -m finance_sync.monitoring.health_monitor
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.parse
from datetime import UTC, datetime
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHECK_INTERVAL = 15 * 60  # 15 minutes between checks
MONITOR_DURATION = 24 * 60 * 60  # 24 hours

# Resource thresholds
CPU_WARN_THRESHOLD = 80.0  # percent
MEM_WARN_THRESHOLD = 80.0  # percent
MEM_CRIT_THRESHOLD = 90.0  # percent

# ── Configuration (env-only, no Hermes fallbacks) ──────────────────────

# Coolify API base URL and the application UUID to monitor.  Both are
# overridable via COOLIFY_API_URL / COOLIFY_APP_UUID.
DEFAULT_COOLIFY_URL = "http://192.168.3.110:8000/api/v1"
DEFAULT_APP_UUID = "obcopz3142hxzs1zlie78amh"

# State file location, overridable via STATE_FILE.  The parent directory
# is created on first save.
DEFAULT_STATE_FILE = "/var/lib/finance-sync/finance-sync-monitor-state.json"

# ── GitHub issue creation constants ──────────────────────────────────────

GITHUB_API_BASE = "https://api.github.com"
GITHUB_OWNER = "rbnbrls"
GITHUB_REPO = "finance-sync"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("finance-sync-monitor")


def get_coolify_token() -> str | None:
    """Return the Coolify API token from ``COOLIFY_API_TOKEN`` (env only)."""
    return os.environ.get("COOLIFY_API_TOKEN")


def get_github_token() -> str | None:
    """Return the GitHub token from ``GITHUB_TOKEN`` (env only)."""
    return os.environ.get(GITHUB_TOKEN_ENV)


def get_state_file() -> str:
    """Return the state file path (``STATE_FILE`` env, sensible default)."""
    return os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)


def get_coolify_url() -> str:
    """Return the Coolify API base URL (``COOLIFY_API_URL`` env)."""
    return os.environ.get("COOLIFY_API_URL", DEFAULT_COOLIFY_URL)


def get_app_uuid() -> str:
    """Return the Coolify application UUID (``COOLIFY_APP_UUID`` env)."""
    return os.environ.get("COOLIFY_APP_UUID", DEFAULT_APP_UUID)


def get_health_base_url() -> str:
    """Return the public base URL for the app health endpoints.

    Defaults to ``https://<app-uuid>.7rb.nl`` (matching the Coolify
    generated domain); override with ``MONITOR_HEALTH_BASE_URL``.
    """
    return os.environ.get("MONITOR_HEALTH_BASE_URL") or (
        f"https://{get_app_uuid()}.7rb.nl"
    )


# ── State management ─────────────────────────────────────────────────────


def load_state() -> dict[str, Any]:
    """Load the monitor state JSON file, or return a fresh state."""
    state_file = get_state_file()
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "started_at": None,
        "checks": [],
        "last_restart_count": -1,
        "last_status": None,
    }


def save_state(state: dict[str, Any]) -> None:
    """Persist the monitor state, creating the parent directory if needed."""
    state_file = get_state_file()
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


# ── Health checks ─────────────────────────────────────────────────────────


def check_health(url: str) -> int:
    """Check the health endpoint.

    Returns the HTTP status code, or 999 on error.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url],
            capture_output=True,
            text=True,
            timeout=15,
        )
        code = (result.stdout or "").strip()
        return int(code) if code else 999
    except Exception:
        return 999


def check_coolify_app() -> dict[str, Any]:
    """Check the Coolify application status via API.

    Uses the Coolify API token (``COOLIFY_API_TOKEN``) in the
    Authorization header.  Returns the current restart count so crash
    detection can compare it against the previous run.
    """
    token = get_coolify_token()
    cmd = ["curl", "-s", f"{get_coolify_url()}/applications/{get_app_uuid()}"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout or "{}")
        return {
            "status": data.get("status", "unknown"),
            "restart_count": data.get("restart_count", -1),
            "last_online": data.get("last_online_at", "never"),
        }
    except Exception as e:
        return {
            "status": f"error: {e}",
            "restart_count": -1,
            "last_online": "error",
        }


def check_container_resources() -> dict[str, Any]:
    """Check container resource usage for finance-sync containers.

    Queries Docker stats for the app and worker containers.
    Returns dict with CPU% and memory% for each container.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        containers: dict[str, Any] = {}
        for line in (result.stdout or "").strip().split("\n"):
            if not line or "finance-sync" not in line:
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                name = parts[0]
                cpu_str = parts[1].rstrip("%")
                mem_str = parts[2].rstrip("%")
                mem_usage = parts[3]
                try:
                    cpu_pct = float(cpu_str)
                except ValueError:
                    cpu_pct = 0.0
                try:
                    mem_pct = float(mem_str)
                except ValueError:
                    mem_pct = 0.0
                containers[name] = {
                    "cpu_percent": cpu_pct,
                    "mem_percent": mem_pct,
                    "mem_usage": mem_usage,
                }
        return containers
    except Exception as e:
        return {"_error": str(e)}


def check_resource_thresholds(resources: dict[str, Any]) -> list[str]:
    """Check resource usage against thresholds.

    Returns a list of alert messages.
    """
    alerts: list[str] = []
    for name, data in resources.items():
        if name.startswith("_") or not isinstance(data, dict):
            continue
        container = cast("dict[str, Any]", data)
        if container.get("cpu_percent", 0) > CPU_WARN_THRESHOLD:
            alerts.append(
                f"  ⚠ {name}: CPU {container['cpu_percent']:.1f}% "
                f"(threshold: {CPU_WARN_THRESHOLD}%)"
            )
        if container.get("mem_percent", 0) > MEM_CRIT_THRESHOLD:
            alerts.append(
                f"  🚨 {name}: Memory {container['mem_percent']:.1f}% "
                f"(CRITICAL threshold: {MEM_CRIT_THRESHOLD}%)"
            )
        elif container.get("mem_percent", 0) > MEM_WARN_THRESHOLD:
            alerts.append(
                f"  ⚠ {name}: Memory {container['mem_percent']:.1f}% "
                f"(warn threshold: {MEM_WARN_THRESHOLD}%)"
            )
    return alerts


# ══════════════════════════════════════════════════════════════════════════
# GitHub issue helpers
# ══════════════════════════════════════════════════════════════════════════


def build_crash_marker(dt: datetime | None = None) -> str:
    """Build the hidden HTML dedup marker for crash events.

    The marker embeds the date so we can search for open issues
    created today for the same event type.
    """
    if dt is None:
        dt = datetime.now(UTC)
    date_str = dt.strftime("%Y-%m-%d")
    return f"<!-- crash-monitor:{date_str} -->"


def build_resource_marker(dt: datetime | None = None) -> str:
    """Build the hidden HTML dedup marker for resource alert events."""
    if dt is None:
        dt = datetime.now(UTC)
    date_str = dt.strftime("%Y-%m-%d")
    return f"<!-- resource-monitor:{date_str} -->"


def build_crash_issue_body(
    timestamp: str,
    app_health: int,
    cf_status: str,
    restart_count: int,
    restarts_changed: bool,
    resources: dict[str, Any],
) -> str:
    """Build the Markdown body for a crash/health check failure issue.

    Args:
        timestamp: ISO-8601 timestamp of the event.
        app_health: HTTP status code from the health endpoint.
        cf_status: Coolify application status.
        restart_count: Current restart count from Coolify.
        restarts_changed: Whether the restart count increased since last check.
        resources: Container resource metrics (from check_container_resources).

    Returns:
        A formatted Markdown issue body with a dedup marker.
    """
    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d")

    restart_note = (
        f"The restart count has **increased** (now {restart_count})."
        if restarts_changed
        else f"Restart count is {restart_count}."
    )

    lines = [
        "## 🚨 Crash / Health Check Failure — finance-sync",
        "",
        f"**Detected at:** {timestamp}",
        f"**Date (UTC):** {date_str}",
        "",
        "### Status",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| App Health Check | HTTP {app_health} |",
        f"| Coolify Status | {cf_status} |",
        f"| Restart Count | {restart_count} |",
        f"| Restarts Changed | {'Yes' if restarts_changed else 'No'} |",
        "",
        "### Details",
        "",
        restart_note,
    ]

    if resources:
        lines.extend(
            [
                "",
                "### Container Resources",
                "",
                "| Container | CPU | Memory | Memory Usage |",
                "|-----------|-----|--------|--------------|",
            ]
        )
        for name, data in resources.items():
            cpu = f"{data.get('cpu_percent', 0):.1f}%"
            mem = f"{data.get('mem_percent', 0):.1f}%"
            usage = data.get("mem_usage", "N/A")
            lines.append(f"| {name} | {cpu} | {mem} | {usage} |")

    # Hidden dedup marker
    lines.extend(
        [
            "",
            f"<!-- crash-monitor:{date_str} -->",
        ]
    )

    return "\n".join(lines)


def build_resource_alert_issue_body(
    alerts: list[str],
    resources: dict[str, Any],
) -> str:
    """Build the Markdown body for a resource threshold alert issue.

    Args:
        alerts: List of alert strings from check_resource_thresholds.
        resources: Container resource metrics.

    Returns:
        A formatted Markdown issue body with a dedup marker.
    """
    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d")

    lines = [
        "## ⚠ Resource Threshold Alert — finance-sync",
        "",
        f"**Detected at:** {now.isoformat()}",
        f"**Date (UTC):** {date_str}",
        "",
        "### Alerts",
        "",
    ]
    lines.extend(f"- {alert.strip()}" for alert in alerts)
    lines.append("")

    if resources:
        lines.extend(
            [
                "### Container Resources",
                "",
                "| Container | CPU | Memory | Memory Usage |",
                "|-----------|-----|--------|--------------|",
            ]
        )
        for name, data in resources.items():
            cpu = f"{data.get('cpu_percent', 0):.1f}%"
            mem = f"{data.get('mem_percent', 0):.1f}%"
            usage = data.get("mem_usage", "N/A")
            lines.append(f"| {name} | {cpu} | {mem} | {usage} |")
        lines.append("")

    # Hidden dedup marker
    lines.append(f"<!-- resource-monitor:{date_str} -->")

    return "\n".join(lines)


def create_github_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str | None:
    """Create a GitHub issue via the REST API.

    Args:
        owner: Repository owner.
        repo: Repository name.
        title: Issue title.
        body: Issue body (Markdown).
        labels: Optional list of label names.

    Returns:
        The issue HTML URL on success, or None on failure.
    """
    token = get_github_token()
    if not token:
        logger.warning("GITHUB_TOKEN not set — cannot create issue")
        return None

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "finance-sync-monitor/1.0",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels

    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=headers, method="POST")

    try:
        with urlopen(request, timeout=30) as response:
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


def check_existing_issue(
    owner: str,
    repo: str,
    marker: str,
) -> bool:
    """Check if an open issue with the given dedup marker already exists.

    Uses the GitHub search API to find open issues containing the marker.
    Returns True if at least one open issue matches within the last 24h.
    Returns False on any error (conservative — skips dedup on error).
    """
    token = get_github_token()
    if not token:
        return False

    # Search for open issues containing the marker in the repo body
    # Using the 'in:body' qualifier to search only issue bodies
    query = f"repo:{owner}/{repo} is:issue is:open {marker} in:body"
    url = f"{GITHUB_API_BASE}/search/issues?q={_urlencode_query(query)}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "finance-sync-monitor/1.0",
    }

    request = Request(url, headers=headers, method="GET")

    try:
        with urlopen(request, timeout=15) as response:
            response_body = response.read()
    except Exception:
        logger.warning("Failed to check existing issues", exc_info=True)
        return False

    try:
        data: dict[str, Any] = json.loads(response_body)
    except (json.JSONDecodeError, ValueError):
        return False

    total_count = data.get("total_count", 0)
    return total_count > 0


def _urlencode_query(query: str) -> str:
    """Simple URL-encode a query string for the GitHub search API.

    We only need to encode spaces and special chars.
    """
    return urllib.parse.quote(query, safe="")


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Run one monitor pass: check health, detect crashes, file issues."""
    state = load_state()
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    if not state.get("started_at"):
        state["started_at"] = now_iso

    # Check app health (primary and worker)
    health_base = get_health_base_url()
    app_health = check_health(f"{health_base}/health/live")
    worker_health = check_health(f"{health_base}/health/ready")

    # Check Coolify status
    cf = check_coolify_app()

    # Detect crashes: check if restart count increased
    restarts_changed = False
    last_count = state.get("last_restart_count", -1)
    if last_count >= 0 and cf["restart_count"] > last_count:
        restarts_changed = True

    check = {
        "timestamp": now_iso,
        "app_health_code": app_health,
        "worker_health_code": worker_health,
        "coolify_status": cf["status"],
        "restart_count": cf["restart_count"],
        "has_crash": restarts_changed or app_health != 200,
    }
    state["checks"].append(check)
    state["last_restart_count"] = cf["restart_count"]
    state["last_status"] = cf["status"]

    crash_detected = restarts_changed or app_health != 200

    # Check container resource usage
    resources = check_container_resources()
    resource_alerts = check_resource_thresholds(resources)

    # ── Create GitHub issues ──────────────────────────────────────────

    github_ok = True  # tracks API health for exit code

    if crash_detected:
        # Check dedup before creating
        crash_marker = build_crash_marker(now)
        if check_existing_issue(GITHUB_OWNER, GITHUB_REPO, crash_marker):
            logger.info("Skipping crash issue — already exists for today")
        else:
            body = build_crash_issue_body(
                timestamp=now_iso,
                app_health=app_health,
                cf_status=cf["status"],
                restart_count=cf["restart_count"],
                restarts_changed=restarts_changed,
                resources=resources,
            )
            result = create_github_issue(
                owner=GITHUB_OWNER,
                repo=GITHUB_REPO,
                title=(
                    f"[Crash] finance-sync — HTTP {app_health}, "
                    f"status={cf['status']}"
                ),
                body=body,
                labels=["bug"],
            )
            if result is None:
                github_ok = False

        msg = [
            "CRASH/HEALTH EVENT DETECTED on finance-sync!",
            f"  Timestamp: {now_iso}",
            f"  App health: HTTP {app_health}",
            f"  Coolify status: {cf['status']}",
            f"  Restart count: {cf['restart_count']}"
            + (" (INCREASED!)" if restarts_changed else ""),
        ]
        if resource_alerts:
            msg.append("  Resource alerts:")
            msg.extend(resource_alerts)
        print("\n".join(msg))
        save_state(state)
        sys.exit(0 if github_ok else 1)

    if resource_alerts:
        resource_marker = build_resource_marker(now)
        if check_existing_issue(GITHUB_OWNER, GITHUB_REPO, resource_marker):
            logger.info(
                "Skipping resource alert issue — already exists for today"
            )
        else:
            body = build_resource_alert_issue_body(
                alerts=resource_alerts,
                resources=resources,
            )
            result = create_github_issue(
                owner=GITHUB_OWNER,
                repo=GITHUB_REPO,
                title=(
                    "[Resource Alert] finance-sync — "
                    "CPU/Memory threshold exceeded"
                ),
                body=body,
                labels=["enhancement"],
            )
            if result is None:
                github_ok = False

    # Print resource status
    resource_line_parts: list[str] = []
    for name, data in resources.items():
        if "_error" not in name:
            resource_line_parts.append(
                f"{name}: CPU {data.get('cpu_percent', 0):.1f}% "
                f"Mem {data.get('mem_percent', 0):.1f}%"
            )
    resource_status = (
        " | ".join(resource_line_parts)
        if resource_line_parts
        else "no containers"
    )

    # Check if we've been monitoring long enough
    started = datetime.fromisoformat(state["started_at"])
    elapsed = (datetime.now(UTC) - started.replace(tzinfo=UTC)).total_seconds()
    elapsed_hours = elapsed / 3600

    print(
        f"[finance-sync-monitor] Health OK | HTTP {app_health} | "
        f"Status: {cf['status']} | Restarts: {cf['restart_count']} | "
        f"Resources: {resource_status} | Elapsed: {elapsed_hours:.1f}h / 24h"
    )
    if resource_alerts:
        print("\nResource threshold alerts:")
        for alert in resource_alerts:
            print(alert)

    if elapsed >= MONITOR_DURATION:
        total_checks = len(state["checks"])
        crashes = [c for c in state["checks"] if c.get("has_crash")]
        final_report = [
            "",
            "=" * 60,
            "FINANCE-SYNC MONITORING COMPLETE (24h)",
            "=" * 60,
            f"Duration: {elapsed_hours:.1f} hours",
            f"Total checks: {total_checks}",
            f"Crash events detected: {len(crashes)}",
            f"Final health code: HTTP {app_health}",
            f"Final Coolify status: {cf['status']}",
            f"Total restarts observed: {cf['restart_count']}",
        ]
        if not crashes and app_health == 200:
            final_report.append(
                "\nRESULT: PASS - No crash events detected in 24h. "
                "Application is healthy."
            )
        else:
            final_report.append(
                "\nRESULT: FAIL - Issues detected during monitoring period."
            )
            final_report.extend(
                f"  - {c['timestamp']}: HTTP {c['app_health_code']}, "
                f"status={c['coolify_status']}"
                for c in crashes
            )
        print("\n".join(final_report))

        state_file = get_state_file()
        if os.path.exists(state_file):
            os.remove(state_file)
        save_state({})  # reset

    save_state(state)

    # Exit with appropriate code
    if not github_ok:
        # API error — exit non-zero so the scheduler knows something went wrong
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
