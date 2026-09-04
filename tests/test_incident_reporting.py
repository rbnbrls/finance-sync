"""Tests for the single-route connector incident reporter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from finance_sync.config.settings import Settings
from finance_sync.services.github_issue import GitHubIssueResult
from finance_sync.services.incident_reporting import (
    incident_fingerprint,
    report_connector_failure,
)


def _settings(token: str = "") -> Settings:
    return Settings(github_token=SecretStr(token), github_repo="owner/repo")


def test_incident_fingerprint_is_stable_and_ignores_volatile_values() -> None:
    first = incident_fingerprint(
        connector="saxo", operation="sync", error=RuntimeError("HTTP 500 id 12345")
    )
    second = incident_fingerprint(
        connector="saxo", operation="sync", error=RuntimeError("HTTP 500 id 67890")
    )
    assert first == second


@pytest.mark.asyncio
async def test_reports_to_github_without_sending_to_glitchtip() -> None:
    service = AsyncMock()
    service.find_open_issue_by_marker.return_value = None
    service.create_issue.return_value = GitHubIssueResult(
        success=True, issue_url="https://github.com/owner/repo/issues/1"
    )
    fallback = MagicMock()
    error = RuntimeError("provider unavailable")

    with patch(
        "finance_sync.services.incident_reporting.GitHubIssueService",
        return_value=service,
    ):
        result = await report_connector_failure(
            _settings("gh-token"),
            error,
            connector="saxo_investor",
            operation="sync_connection",
            fallback_capture=fallback,
        )

    assert result["channel"] == "github"
    service.create_issue.assert_awaited_once()
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_deduplicates_existing_github_issue() -> None:
    service = AsyncMock()
    service.find_open_issue_by_marker.return_value = {
        "html_url": "https://github.com/owner/repo/issues/8"
    }
    fallback = AsyncMock()

    with patch(
        "finance_sync.services.incident_reporting.GitHubIssueService",
        return_value=service,
    ):
        result = await report_connector_failure(
            _settings("gh-token"),
            RuntimeError("provider unavailable"),
            connector="saxo_investor",
            operation="sync_connection",
            fallback_capture=fallback,
        )

    assert result["status"] == "deduplicated"
    service.create_issue.assert_not_awaited()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_falls_back_to_glitchtip_when_github_is_unavailable() -> None:
    fallback = MagicMock()

    result = await report_connector_failure(
        _settings(),
        RuntimeError("provider unavailable"),
        connector="saxo_investor",
        operation="sync_connection",
        fallback_capture=fallback,
    )

    assert result["channel"] == "glitchtip"
    fallback.assert_called_once()
