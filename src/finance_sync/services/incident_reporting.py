"""Single-route incident reporting for connector and delivery failures."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

from finance_sync.config.settings import Settings, secret_value
from finance_sync.observability.glitchtip import capture_connector_exception
from finance_sync.services.github_issue import GitHubIssueService
from finance_sync.utils.redaction import redact_text

if TYPE_CHECKING:
    from collections.abc import Callable

_VOLATILE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b\d{4,}\b", re.IGNORECASE
)


def incident_fingerprint(
    *, connector: str, operation: str, error: BaseException
) -> str:
    """Create a stable, non-sensitive key for one recurring failure class."""
    message = _VOLATILE.sub("<value>", str(error).strip().lower())
    raw = (
        f"{connector.lower()}|{operation.lower()}|"
        f"{type(error).__name__}|{message}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


async def report_connector_failure(
    settings: Settings,
    error: BaseException,
    *,
    connector: str,
    operation: str,
    connection_id: str | None = None,
    provider_account_id: str | None = None,
    correlation_id: str | None = None,
    fallback_capture: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Report exactly once via GitHub, or GlitchTip as the fallback.

    Direct GitHub delivery is attempted first.  GlitchTip is called only when
    GitHub is not configured or cannot accept the incident, preventing the
    same failure from being sent to both systems by this process.
    """
    fingerprint = incident_fingerprint(
        connector=connector, operation=operation, error=error
    )
    token = secret_value(settings.github_token)
    if token and "/" in settings.github_repo:
        owner, repo = settings.github_repo.split("/", 1)
        marker = f"finance-sync-incident:{fingerprint}"
        service = GitHubIssueService(token=token)
        existing = await service.find_open_issue_by_marker(
            owner=owner, repo=repo, marker=marker
        )
        if existing is not None:
            return {
                "channel": "github",
                "status": "deduplicated",
                "fingerprint": fingerprint,
                "issue_url": existing.get("html_url"),
            }
        safe_error = redact_text(str(error))[:500]
        result = await service.create_issue(
            owner=owner,
            repo=repo,
            title=f"[Connector failure] {connector}: {operation}",
            body=(
                f"<!-- {marker} -->\n"
                "## Connector failure\n\n"
                f"- Connector: `{connector}`\n"
                f"- Operation: `{operation}`\n"
                f"- Error type: `{type(error).__name__}`\n"
                f"- Error: `{safe_error}`\n\n"
                "This issue is generated automatically. Resolve the root "
                "cause, then rerun the affected sync."
            ),
            labels=["bug", "area:connector"],
        )
        if result.success:
            return {
                "channel": "github",
                "status": "created",
                "fingerprint": fingerprint,
                "issue_url": result.issue_url,
            }

    # The GlitchTip event carries the same fingerprint so its bridge can
    # group recurring failures when direct GitHub delivery is unavailable.
    (fallback_capture or capture_connector_exception)(
        error,
        connector=connector,
        operation=operation,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        correlation_id=correlation_id,
        fingerprint=fingerprint,
    )
    return {
        "channel": "glitchtip",
        "status": "captured",
        "fingerprint": fingerprint,
    }
