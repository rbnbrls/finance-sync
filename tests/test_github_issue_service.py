"""Unit tests for GitHubIssueService.

Uses ``httpx.MockTransport`` to simulate GitHub API responses
without making real HTTP calls.
"""
# pyright: basic

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from finance_sync.services.github_issue import GitHubIssueService

# ── Helpers ────────────────────────────────────────────────────────────


def _make_service(
    handler: Any,
    token: str = "ghp_test_valid_token_12345",
) -> GitHubIssueService:
    """Build a service wired to a mock transport."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return GitHubIssueService(token=token, http_client=client)


def _success_json() -> dict[str, Any]:
    return {
        "html_url": "https://github.com/rbnbrls/finance-sync/issues/42",
        "number": 42,
        "title": "[BUG] Test issue",
        "state": "open",
    }


class TestCreateIssue:
    """Tests for ``GitHubIssueService.create_issue``."""

    # ── Success ─────────────────────────────────────────────────────

    async def test_success(self) -> None:
        """A 201 response returns the issue URL and number."""

        async def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization", "")
            assert "Bearer ghp_test_valid_token_12345" in str(auth)
            accept = "application/vnd.github.v3+json"
            assert request.headers.get("Accept") == accept
            body = json.loads(await request.aread())
            assert body["title"] == "[BUG] Login button missing on mobile"
            assert body["body"] == "## Steps\n1. Open app"
            assert body["labels"] == ["bug", "feedback"]
            return httpx.Response(201, json=_success_json())

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="[BUG] Login button missing on mobile",
            body="## Steps\n1. Open app",
            labels=["bug", "feedback"],
        )

        assert result.success is True
        expected = "https://github.com/rbnbrls/finance-sync/issues/42"
        assert result.issue_url == expected
        assert result.issue_number == 42
        assert result.error is None
        assert result.status_code is None

    async def test_success_without_labels(self) -> None:
        """Omitting labels should not send the field."""

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(await request.aread())
            assert "labels" not in body
            return httpx.Response(201, json=_success_json())

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="Test",
            body="Body",
        )

        assert result.success is True

    # ── Authentication errors ──────────────────────────────────────

    @pytest.mark.parametrize(
        ("status", "expected_error_substr"),
        [
            (401, "authentication"),
            (403, "authentication"),
        ],
    )
    async def test_auth_error(
        self,
        status: int,
        expected_error_substr: str,
    ) -> None:
        """401 and 403 without rate-limit signal return auth error."""

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"message": "Bad credentials"})

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="T",
            body="B",
        )

        assert result.success is False
        assert result.issue_url is None
        assert expected_error_substr in (result.error or "").lower()
        assert result.status_code == status

    # ── Rate limit errors ──────────────────────────────────────────

    @pytest.mark.parametrize(
        ("status", "headers"),
        [
            (429, {}),
            (
                403,
                {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1712345678",
                },
            ),
        ],
    )
    async def test_rate_limit(
        self,
        status: int,
        headers: dict[str, str],
    ) -> None:
        """429 and 403 with remaining=0 should be classified as rate limit."""

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                json={"message": "API rate limit exceeded"},
                headers=headers,
            )

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="T",
            body="B",
        )

        assert result.success is False
        assert "rate limit" in (result.error or "").lower()
        assert result.status_code == status

    # ── Not found ──────────────────────────────────────────────────

    async def test_not_found(self) -> None:
        """404 returns a not-found error."""

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="nonexistent",
            title="T",
            body="B",
        )

        assert result.success is False
        assert "not found" in (result.error or "").lower()
        assert result.status_code == 404

    # ── Validation errors ──────────────────────────────────────────

    async def test_validation_error(self) -> None:
        """422 returns validation details."""

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [
                        {
                            "resource": "Issue",
                            "field": "title",
                            "code": "missing",
                        },
                    ],
                },
            )

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="",
            body="B",
        )

        assert result.success is False
        assert "validation" in (result.error or "").lower()
        assert "missing" in (result.error or "")
        assert result.status_code == 422

    async def test_validation_error_no_errors_field(self) -> None:
        """422 without 'errors' list still produces a message."""

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"message": "Validation Failed"})

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="",
            body="",
        )

        assert result.success is False
        assert "422" in (result.error or "")
        assert result.status_code == 422

    # ── Server errors ──────────────────────────────────────────────

    @pytest.mark.parametrize("status_code", [500, 502, 503])
    async def test_server_error(self, status_code: int) -> None:
        """5xx returns a server error."""

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                json={"message": "Internal Server Error"},
            )

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="T",
            body="B",
        )

        assert result.success is False
        assert str(status_code) in (result.error or "")
        assert result.status_code == status_code

    # ── Network / transport errors ─────────────────────────────────

    async def test_timeout(self) -> None:
        """A timeout produces a descriptive error."""

        async def handler(_: httpx.Request) -> httpx.Response:
            msg = "timeout"
            raise httpx.TimeoutException(msg)

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="T",
            body="B",
        )

        assert result.success is False
        assert "timed out" in (result.error or "").lower()
        assert result.status_code is None

    async def test_network_error(self) -> None:
        """A connection error produces a descriptive error."""

        async def handler(_: httpx.Request) -> httpx.Response:
            msg = "Connection refused"
            raise httpx.ConnectError(msg)

        service = _make_service(handler)
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="T",
            body="B",
        )

        assert result.success is False
        assert "network" in (result.error or "").lower()
        assert result.status_code is None

    # ── No injected client ─────────────────────────────────────────

    async def test_no_injected_client_uses_short_lived(self) -> None:
        """When no client is injected, the service creates its own."""
        service = GitHubIssueService(token="ghp_test_token")
        # This would actually hit the network, but the error will be
        # a connection error because there's no real GitHub.  We just
        # verify the code path doesn't crash with a different error.
        result = await service.create_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="T",
            body="B",
        )
        assert result.success is False
        assert result.error is not None

    # ── Direct _do_post (static helper) ────────────────────────────

    async def test_do_post_handles_unexpected_error(self) -> None:
        """_do_post catches httpx.RequestError and returns a result."""
        err_msg = "Connection lost"

        def raiser(_: object) -> httpx.Response:
            raise httpx.RemoteProtocolError(err_msg)

        client = httpx.AsyncClient(transport=httpx.MockTransport(raiser))
        result = await GitHubIssueService._do_post(
            client,
            "https://api.github.com/repos/owner/repo/issues",
            {"Authorization": "Bearer test"},
            {"title": "T", "body": "B"},
        )

        assert result.success is False
        assert "network" in (result.error or "").lower()
