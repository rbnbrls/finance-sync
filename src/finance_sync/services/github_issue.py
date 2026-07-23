"""GitHub issue creation service.

Provides a reusable service for creating GitHub issues via the
GitHub Issues API (POST /repos/{owner}/{repo}/issues).  Handles
authentication errors, rate limits, validation errors, and server
errors gracefully as structured ``GitHubIssueResult`` objects.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ── Exceptions ─────────────────────────────────────────────────────


class GitHubError(Exception):
    """Base exception for GitHub API errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class GitHubAuthError(GitHubError):
    """Authentication or authorisation failure (401 / 403)."""


class GitHubRateLimitError(GitHubError):
    """Rate limit exceeded (429 or 403 with rate-limit headers)."""


class GitHubNotFoundError(GitHubError):
    """Repository or resource not found (404)."""


class GitHubValidationError(GitHubError):
    """Request validation failed (422)."""


class GitHubServerError(GitHubError):
    """GitHub API server error (5xx)."""


# ── Result types ───────────────────────────────────────────────────


@dataclass
class GitHubIssueResult:
    """Structured outcome of a GitHub issue creation attempt.

    Attributes:
        success: Whether the issue was created successfully.
        issue_url: URL of the created issue (on success).
        issue_number: Issue number (on success).
        error: Human-readable error description (on failure).
        status_code: HTTP status code from the GitHub API (on failure).
    """

    success: bool
    issue_url: str | None = None
    issue_number: int | None = None
    error: str | None = None
    status_code: int | None = None


# ── Error classification helpers ───────────────────────────────────


def _classify_error(
    response: httpx.Response,
) -> GitHubError:
    """Map an httpx response to a specific ``GitHubError`` subclass.

    Args:
        response: The failed HTTP response from the GitHub API.

    Returns:
        A ``GitHubError`` subclass instance with a descriptive message.
    """
    status_code = response.status_code
    body: str | None = None
    with suppress(Exception):
        body = response.text

    rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
    is_rate_limit = status_code == 429 or (
        status_code == 403 and rate_limit_remaining == "0"
    )

    if is_rate_limit:
        reset_epoch = response.headers.get("X-RateLimit-Reset", "unknown")
        msg = (
            f"GitHub API rate limit exceeded. "
            f"Resets at timestamp: {reset_epoch}. "
            f"Body: {body or 'N/A'}"
        )
        return GitHubRateLimitError(msg, status_code=status_code)

    if status_code in (401, 403):
        msg = (
            f"GitHub authentication failed ({status_code}). "
            f"Check that GITHUB_TOKEN is valid and has the 'public_repo' "
            f"or 'repo' scope. Body: {body or 'N/A'}"
        )
        return GitHubAuthError(msg, status_code=status_code)

    if status_code == 404:
        msg = (
            f"GitHub repository not found ({status_code}). "
            f"Check that the repository exists and is accessible "
            f"with the configured token. Body: {body or 'N/A'}"
        )
        return GitHubNotFoundError(msg, status_code=status_code)

    if status_code == 422:
        errors = _extract_validation_errors(response)
        msg = (
            f"GitHub request validation failed ({status_code}): "
            f"{errors}. Body: {body or 'N/A'}"
        )
        return GitHubValidationError(msg, status_code=status_code)

    # Fallback: 5xx or anything else
    msg = f"GitHub API error ({status_code}). Body: {body or 'N/A'}"
    return GitHubServerError(msg, status_code=status_code)


def _extract_validation_errors(response: httpx.Response) -> str:
    """Extract validation error details from a 422 response."""
    try:
        data = response.json()
        errors = data.get("errors", [])
        if errors:
            return "; ".join(e.get("message", str(e)) for e in errors)
        return data.get("message", "Unknown validation error")
    except Exception:
        return "Could not parse validation errors"


# ── Service ────────────────────────────────────────────────────────


class GitHubIssueService:
    """Service for creating GitHub issues.

    Uses the GitHub Issues API (``POST /repos/{owner}/{repo}/issues``)
    with a personal access token for authentication.

    Usage::

        service = GitHubIssueService(token="ghp_...")
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="Bug: login broken on mobile",
            body="## Steps to reproduce...",
            labels=["bug"],
        )
        if result.success:
            print(f"Issue created: {result.issue_url}")
        else:
            print(f"Failed: {result.error}")
    """

    BASE_URL = "https://api.github.com"
    USER_AGENT = "finance-sync/0.1.0"
    DEFAULT_TIMEOUT = 15.0

    def __init__(
        self,
        token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            token: GitHub personal access token with ``public_repo`` or
                ``repo`` scope.
            http_client: Optional pre-configured ``httpx.AsyncClient``.
                When omitted a short-lived client is created and closed
                within each ``create_issue`` call.
        """
        self._token = token
        self._http_client = http_client

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> GitHubIssueResult:
        """Create a GitHub issue.

        Args:
            owner: Repository owner (user or organisation).
            repo: Repository name.
            title: Issue title.
            body: Issue body (Markdown).
            labels: Optional list of label names to apply.

        Returns:
            A ``GitHubIssueResult`` with the outcome.
        """
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues"

        payload: dict[str, Any] = {
            "title": title,
            "body": body,
        }
        if labels:
            payload["labels"] = labels

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": self.USER_AGENT,
        }

        # Use the injected client when available so tests can supply
        # their own transport layer.  Otherwise create a short-lived
        # client for this one request.
        if self._http_client is not None:
            return await self._do_post(self._http_client, url, headers, payload)

        async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
            return await self._do_post(client, url, headers, payload)

    @staticmethod
    async def _do_post(
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> GitHubIssueResult:
        """Execute the POST request and process the response."""
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            logger.warning("github_issue_timeout", url=url)
            return GitHubIssueResult(
                success=False,
                error="Request timed out while connecting to GitHub API.",
                status_code=None,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "github_issue_request_error", url=url, error=str(exc)
            )
            return GitHubIssueResult(
                success=False,
                error=f"Network error contacting GitHub API: {exc}",
                status_code=None,
            )

        if response.is_error:
            gh_error = _classify_error(response)
            logger.warning(
                "github_issue_failed",
                url=url,
                status_code=response.status_code,
                error=str(gh_error),
            )
            return GitHubIssueResult(
                success=False,
                error=str(gh_error),
                status_code=response.status_code,
            )

        data = response.json()
        issue_url = data.get("html_url")
        issue_number = data.get("number")

        logger.info(
            "github_issue_created",
            url=url,
            issue_number=issue_number,
            issue_url=issue_url,
        )
        return GitHubIssueResult(
            success=True,
            issue_url=issue_url,
            issue_number=issue_number,
        )
