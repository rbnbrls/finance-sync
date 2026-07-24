"""Feedback endpoint — bug reports / feature requests as GitHub issues.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

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
