"""Feedback endpoint — submits bug reports and feature requests as GitHub issues.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from finance_sync.api.deps.auth import get_current_user
from finance_sync.dependencies import get_settings
from finance_sync.models.user import User as UserModel

router = APIRouter(prefix="/feedback", tags=["feedback"])

FEEDBACK_LABELS = {
    "bug": "bug",
    "feature": "enhancement",
}


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
            detail=f"Invalid type: {feedback_type!r}. Must be 'bug' or 'feature'.",
        )

    if not settings.github_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub integration is not configured (GITHUB_TOKEN is missing).",
        )

    # ── Build GitHub issue body ─────────────────────────────────────
    label = FEEDBACK_LABELS[feedback_type]
    issue_body = (
        f"### Feedback from {user.email}\n\n"
        f"**Type:** {feedback_type}\n"
        f"**User:** {user.email}\n\n"
        f"---\n\n"
        f"{description}"
    )

    # ── Call GitHub API ─────────────────────────────────────────────
    github_api_url = (
        f"https://api.github.com/repos/{settings.github_repo}/issues"
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            github_api_url,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "finance-sync/0.1.0",
            },
            json={
                "title": f"[{label.upper()}] {title}",
                "body": issue_body,
                "labels": [label],
            },
        )

    if resp.is_error:
        detail = (
            f"Failed to create GitHub issue: {resp.status_code} "
            f"{resp.text[:500]}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )

    issue_data = resp.json()
    return {
        "success": True,
        "issue_url": issue_data.get("html_url"),
        "issue_number": issue_data.get("number"),
        "message": "Feedback submitted successfully. Thank you!",
    }
