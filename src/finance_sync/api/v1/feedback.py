"""Feedback endpoint — bug reports / feature requests as GitHub issues.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import Any

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from finance_sync.api.deps.auth import get_current_user
from finance_sync.dependencies import get_settings
from finance_sync.models.user import User as UserModel
from finance_sync.services.github_issue import GitHubIssueService

router = APIRouter(prefix="/feedback", tags=["feedback"])

FEEDBACK_LABELS = {
    "bug": "bug",
    "feature": "enhancement",
}
_DEFAULT_LABEL = "feedback"


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    request: Request,
    body: dict[str, Any],
    user: UserModel = Depends(get_current_user),
) -> dict[str, Any]:
    """Submit feedback (bug report or feature request).

    Creates a GitHub issue in the repository configured via
    ``GITHUB_REPO`` and ``GITHUB_TOKEN`` environment variables.
    """
    settings = get_settings(request)

    title = (body.get("title") or "").strip()
    feedback_type = body.get("type", "feature")
    description = (body.get("description") or "").strip()

    # ── Validation ──────────────────────────────────────────────────
    if not title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Title is required.",
        )
    if not description:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Description is required.",
        )
    if feedback_type not in FEEDBACK_LABELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid type: {feedback_type!r}. Must be 'bug' or 'feature'."
            ),
        )

    if not settings.github_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "GitHub integration is not configured"
                " (GITHUB_TOKEN is missing)."
            ),
        )

    # ── Parse repo into owner / name ─────────────────────────────────
    repo_full = settings.github_repo
    if "/" not in repo_full:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Invalid GITHUB_REPO format: {repo_full!r}. "
                "Expected owner/repo."
            ),
        )
    owner, repo_name = repo_full.split("/", 1)

    # ── Build GitHub issue body ─────────────────────────────────────
    label = FEEDBACK_LABELS[feedback_type]
    issue_body = (
        f"### Feedback from {user.email}\n\n"
        f"**Type:** {feedback_type}\n"
        f"**User:** {user.email}\n\n"
        f"---\n\n"
        f"{description}"
    )

    # ── Call the service ─────────────────────────────────────────────
    service = GitHubIssueService(token=settings.github_token)
    title_with_prefix = f"[{label.upper()}] {title}"

    # Append feedback label so issues are also discoverable
    result = await service.create_issue(
        owner=owner,
        repo=repo_name,
        title=title_with_prefix,
        body=issue_body,
        labels=[label, _DEFAULT_LABEL],
    )

    if not result.success:
        raise HTTPException(
            status_code=(
                status.HTTP_502_BAD_GATEWAY
                if result.status_code is not None
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=result.error,
        )

    return {
        "success": True,
        "issue_url": result.issue_url,
        "issue_number": result.issue_number,
        "message": "Feedback submitted successfully. Thank you!",
    }


# ── Client error reporting ─────────────────────────────────────────────


class ClientErrorReport(BaseModel):
    """Payload for frontend-side error reports."""

    message: str = Field(description="Error message string")
    url: str = Field(default="", description="The URL where the error occurred")
    line: int | None = Field(default=None, description="Line number")
    col: int | None = Field(default=None, description="Column number")
    stack: str | None = Field(default=None, description="Stack trace")
    user_agent: str | None = Field(default=None, description="Browser user-agent string")
    context: dict[str, Any] | None = Field(
        default=None,
        description="Additional context (page, action, component, …)",
    )


@router.post("/client-error", status_code=status.HTTP_202_ACCEPTED)
async def report_client_error(
    request: Request,
    body: ClientErrorReport,
    user: UserModel = Depends(get_current_user),
) -> dict[str, Any]:
    """Receive a frontend-side error and create a GitHub issue.

    The endpoint accepts structured error reports from the browser
    (via ``window.onerror`` / ``unhandledrejection`` handlers) and
    creates a GitHub issue in the configured repository for triage.
    """
    settings = get_settings(request)

    if not settings.github_token:
        # Silent discard when GitHub integration is not configured
        return {"success": True, "note": "GitHub integration not configured — error discarded"}

    repo_full = settings.github_repo
    if "/" not in repo_full:
        return {"success": False, "note": "Invalid GITHUB_REPO format"}

    owner, repo_name = repo_full.split("/", 1)

    # Build a rich issue body
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    issue_body = (
        f"## 🐛 Frontend Error Report\n\n"
        f"**Reported:** {now_str}\n"
        f"**User:** {user.email} (`{user.id}`)\n"
        f"**URL:** {body.url or 'N/A'}\n"
        f"**User-Agent:** {body.user_agent or 'N/A'}\n\n"
        f"### Error\n\n"
        f"```\n{body.message}\n```\n\n"
    )
    if body.stack:
        issue_body += (
            f"### Stack Trace\n\n"
            f"```\n{body.stack}\n```\n\n"
        )
    if body.line is not None or body.col is not None:
        issue_body += f"**Location:** line {body.line}, column {body.col}\n\n"
    if body.context:
        import json as _json
        issue_body += (
            f"### Context\n\n"
            f"```json\n{_json.dumps(body.context, indent=2, default=str)}\n```\n"
        )

    title = f"[FRONTEND] {body.message[:120]}"

    service = GitHubIssueService(token=settings.github_token)
    result = await service.create_issue(
        owner=owner,
        repo=repo_name,
        title=title,
        body=issue_body,
        labels=["bug", "frontend"],
    )

    if not result.success:
        return {
            "success": False,
            "note": result.error,
        }

    return {
        "success": True,
        "issue_url": result.issue_url,
        "issue_number": result.issue_number,
    }
