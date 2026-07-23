"""Integration tests for POST /api/v1/feedback endpoint.

Tests the full request → validation → GitHubIssueService → response pipeline.
Uses TestClient with dependency overrides to avoid needing a real database
and patches GitHubIssueService to avoid real API calls.
"""

# pyright: basic

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from finance_sync.api.deps.auth import get_current_user
from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.services.github_issue import GitHubIssueResult

_TEST_SECRET: SecretStr = SecretStr("test-secret-key-16chars!!")

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_user() -> MagicMock:
    """Create a mock user that satisfies get_current_user."""
    user = MagicMock()
    user.email = "test@example.com"
    user.id = "test-user-id"
    user.tenant_id = "test-tenant-id"
    user.role = "user"
    user.is_active = True
    return user


@pytest.fixture
def app(mock_user: MagicMock) -> ...:
    """Build a test app with auth bypassed and GitHub configured."""
    settings = Settings(
        database_url=None,
        redis_url=None,
        secret_key=_TEST_SECRET,
        github_token="ghp_test_token_12345",
        github_repo="rbnbrls/finance-sync",
    )
    app = create_app(settings=settings)

    # Override auth so we don't need a real DB / JWT
    async def _mock_get_current_user():
        return mock_user

    app.dependency_overrides[get_current_user] = _mock_get_current_user
    return app


@pytest.fixture
def client(app: ...) -> ...:
    """FastAPI TestClient that triggers lifespan events."""
    with TestClient(app) as c:
        yield c


# ── Tests ───────────────────────────────────────────────────────────────


class TestSubmitFeedback:
    """POST /api/v1/feedback — issue creation endpoint."""

    def test_submit_bug_report_creates_issue(
        self, client: TestClient,
    ) -> None:
        """Submitting a valid bug report calls the service and returns 201."""
        with patch(
            "finance_sync.api.v1.feedback.GitHubIssueService.create_issue",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = GitHubIssueResult(
                success=True,
                issue_url=(
                    "https://github.com/rbnbrls/finance-sync/issues/99"
                ),
                issue_number=99,
            )

            resp = client.post(
                "/api/v1/feedback",
                json={
                    "title": "Login button missing",
                    "type": "bug",
                    "description": (
                        "The login button does not appear on mobile."
                    ),
                },
                headers={"Authorization": "Bearer test-token"},
            )

            assert resp.status_code == 201
            data = resp.json()
            assert data["success"] is True
            expected = "https://github.com/rbnbrls/finance-sync/issues/99"
            assert data["issue_url"] == expected
            assert data["issue_number"] == 99

            # Verify the title is prefixed with label
            call_kwargs = mock_create.call_args[1]
            assert call_kwargs["title"] == "[BUG] Login button missing"
            assert call_kwargs["labels"] == ["bug", "feedback"]
            assert call_kwargs["owner"] == "rbnbrls"
            assert call_kwargs["repo"] == "finance-sync"

    def test_submit_feature_request_creates_issue(
        self, client: TestClient,
    ) -> None:
        """Feature request uses the 'enhancement' label."""
        with patch(
            "finance_sync.api.v1.feedback.GitHubIssueService.create_issue",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = GitHubIssueResult(
                success=True,
                issue_url=(
                    "https://github.com/rbnbrls/finance-sync/issues/100"
                ),
                issue_number=100,
            )

            resp = client.post(
                "/api/v1/feedback",
                json={
                    "title": "Dark mode toggle",
                    "type": "feature",
                    "description": "Add a dark mode toggle switch.",
                },
                headers={"Authorization": "Bearer test-token"},
            )

            assert resp.status_code == 201
            data = resp.json()
            assert data["success"] is True
            assert data["issue_number"] == 100

            call_kwargs = mock_create.call_args[1]
            assert call_kwargs["title"] == "[ENHANCEMENT] Dark mode toggle"
            assert "enhancement" in call_kwargs["labels"]
            assert "feedback" in call_kwargs["labels"]

    def test_submit_without_title_returns_422(
        self, client: TestClient,
    ) -> None:
        """Missing title returns 422 Unprocessable Entity."""
        resp = client.post(
            "/api/v1/feedback",
            json={"type": "bug", "description": "Some description"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422
        assert "Title" in resp.json()["detail"]

    def test_submit_without_description_returns_422(
        self, client: TestClient,
    ) -> None:
        """Missing description returns 422 Unprocessable Entity."""
        resp = client.post(
            "/api/v1/feedback",
            json={"title": "Test", "type": "feature"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422
        assert "Description" in resp.json()["detail"]

    def test_submit_invalid_type_returns_422(
        self, client: TestClient,
    ) -> None:
        """An unrecognised type value returns 422."""
        resp = client.post(
            "/api/v1/feedback",
            json={
                "title": "Test",
                "type": "invalid",
                "description": "Some desc",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422
        assert "Invalid type" in resp.json()["detail"]

    def test_service_error_returns_502(
        self, client: TestClient,
    ) -> None:
        """When GitHubIssueService returns failure, a 502 is returned."""
        with patch(
            "finance_sync.api.v1.feedback.GitHubIssueService.create_issue",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = GitHubIssueResult(
                success=False,
                error="GitHub API error (422). Body: ...",
                status_code=422,
            )

            resp = client.post(
                "/api/v1/feedback",
                json={
                    "title": "Test",
                    "type": "bug",
                    "description": "Desc",
                },
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 502
            assert "GitHub API error" in resp.json()["detail"]

    def test_service_network_error_returns_503(
        self, client: TestClient,
    ) -> None:
        """When the service has no status_code (network failure), 503."""
        with patch(
            "finance_sync.api.v1.feedback.GitHubIssueService.create_issue",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = GitHubIssueResult(
                success=False,
                error="Network error contacting GitHub API",
                status_code=None,
            )

            resp = client.post(
                "/api/v1/feedback",
                json={
                    "title": "Test",
                    "type": "bug",
                    "description": "Desc",
                },
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 503
            assert "Network error" in resp.json()["detail"]

    def test_issue_body_includes_user_email(
        self, client: TestClient, mock_user: MagicMock,
    ) -> None:
        """The issue body should include the authenticated user's email."""
        mock_user.email = "reporter@example.com"

        with patch(
            "finance_sync.api.v1.feedback.GitHubIssueService.create_issue",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = GitHubIssueResult(
                success=True,
                issue_url=(
                    "https://github.com/rbnbrls/finance-sync/issues/101"
                ),
                issue_number=101,
            )

            resp = client.post(
                "/api/v1/feedback",
                json={
                    "title": "Issue with email",
                    "type": "bug",
                    "description": "Description text.",
                },
                headers={"Authorization": "Bearer test-token"},
            )

            assert resp.status_code == 201
            call_kwargs = mock_create.call_args[1]
            body: str = call_kwargs["body"]
            assert "reporter@example.com" in body
            assert "Description text." in body
            assert "**Type:** bug" in body
