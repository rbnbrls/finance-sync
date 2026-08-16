"""Tests for the Wealthfolio HTTP API client.

Follows TDD: RED phase — write failing tests first.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import RequestError

from finance_sync.exporter.wealthfolio.client import (
    WealthfolioAuthError,
    WealthfolioClient,
    WealthfolioClientConfig,
)

# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client_config() -> WealthfolioClientConfig:
    return WealthfolioClientConfig(
        base_url="http://192.168.3.50:8080",
        password="test-password",
        request_timeout=30.0,
    )


@pytest.fixture
def client(client_config: WealthfolioClientConfig) -> WealthfolioClient:
    return WealthfolioClient(config=client_config)


# ═══════════════════════════════════════════════════════════════════════
# Config tests
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioClientConfig:
    def test_default_config(self) -> None:
        """Default config should have sensible defaults."""
        config = WealthfolioClientConfig(
            base_url="http://localhost:8080",
            password="secret",
        )
        assert config.base_url == "http://localhost:8080"
        assert config.password == "secret"
        assert config.request_timeout == 60.0
        assert config.verify_ssl is True

    def test_config_requires_base_url(self) -> None:
        """Config requires a non-empty base_url."""
        with pytest.raises(ValueError, match="base_url"):
            WealthfolioClientConfig(base_url="", password="secret")

    def test_config_requires_password(self) -> None:
        """Config requires a non-empty password."""
        with pytest.raises(ValueError, match="password"):
            WealthfolioClientConfig(
                base_url="http://localhost:8080", password=""
            )


# ═══════════════════════════════════════════════════════════════════════
# Authentication tests
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioClientAuth:
    async def test_authenticate_success(
        self, client: WealthfolioClient
    ) -> None:
        """Successful authentication should set is_authenticated = True."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "authenticated": True,
            "expiresIn": 86400,
        }
        mock_response.cookies = {"session": "jwt_token_value"}

        with patch.object(client._client, "post") as mock_post:
            mock_post.return_value = mock_response
            result = await client.authenticate()

        assert result is True
        assert client.is_authenticated is True
        # Verify correct endpoint was called
        mock_post.assert_called_once_with(
            "/api/v1/auth/login",
            json={"password": "test-password"},
        )

    async def test_authenticate_failure(
        self, client: WealthfolioClient
    ) -> None:
        """Failed authentication should raise WealthfolioAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {
            "code": 401,
            "message": "Invalid password",
        }
        mock_response.raise_for_status.side_effect = Exception("401")

        with patch.object(client._client, "post") as mock_post:
            mock_post.return_value = mock_response
            with pytest.raises(
                WealthfolioAuthError, match="Authentication failed"
            ):
                await client.authenticate()

        assert client.is_authenticated is False

    async def test_authenticate_connection_error(
        self, client: WealthfolioClient
    ) -> None:
        """Connection error should raise WealthfolioAuthError."""
        with patch.object(client._client, "post") as mock_post:
            mock_post.side_effect = RequestError("Connection refused")
            with pytest.raises(WealthfolioAuthError, match="Connection failed"):
                await client.authenticate()

        assert client.is_authenticated is False

    async def test_auth_status_requires_password(
        self, client: WealthfolioClient
    ) -> None:
        """Auth status endpoint should tell us password is required."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "requiresPassword": True,
            "oidcEnabled": False,
        }

        with patch.object(client._client, "get") as mock_get:
            mock_get.return_value = mock_response
            status = await client.check_auth_status()

        assert status["requiresPassword"] is True
        assert status["oidcEnabled"] is False
        mock_get.assert_called_once_with("/api/v1/auth/status")


# ═══════════════════════════════════════════════════════════════════════
# Import activities tests
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioClientImport:
    async def test_import_activities_success(
        self, client: WealthfolioClient
    ) -> None:
        """Successfully import activities."""
        client._is_authenticated = True
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "imported": 5,
            "skipped": 0,
            "failed": 0,
        }

        activities = [
            {
                "accountId": "acct_123",
                "activityType": "BUY",
                "symbol": "AAPL",
                "quantity": 10,
                "unitPrice": 150.50,
                "amount": 1505.00,
                "currency": "USD",
                "date": "2025-06-15",
            }
        ]

        with patch.object(client._client, "post") as mock_post:
            mock_post.return_value = mock_response
            result = await client.import_activities(activities)

        assert result["imported"] == 5
        mock_post.assert_called_once_with(
            "/api/v1/activities/import",
            json={"activities": activities},
        )

    async def test_import_activities_not_authenticated(
        self, client: WealthfolioClient
    ) -> None:
        """Importing without auth should raise."""
        with pytest.raises(WealthfolioAuthError, match="Not authenticated"):
            await client.import_activities([])

    async def test_check_import_success(
        self, client: WealthfolioClient
    ) -> None:
        """Check import before pushing."""
        client._is_authenticated = True
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "valid": True,
            "issues": [],
        }

        with patch.object(client._client, "post") as mock_post:
            mock_post.return_value = mock_response
            result = await client.check_activities_import(
                [{"activityType": "DEPOSIT", "amount": 1000}]
            )

        assert result["valid"] is True

    async def test_get_accounts(self, client: WealthfolioClient) -> None:
        """Fetch accounts from Wealthfolio."""
        client._is_authenticated = True
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": "acct_1", "name": "Brokerage", "currency": "USD"},
            {"id": "acct_2", "name": "Cash", "currency": "EUR"},
        ]

        with patch.object(client._client, "get") as mock_get:
            mock_get.return_value = mock_response
            accounts = await client.get_accounts()

        assert len(accounts) == 2
        assert accounts[0]["name"] == "Brokerage"
        mock_get.assert_called_once_with("/api/v1/accounts")

    async def test_ensure_account_uses_stable_provider_identity(
        self, client: WealthfolioClient
    ) -> None:
        client._is_authenticated = True
        existing = {
            "id": "wf-pension",
            "name": "DEGIRO Pensioen",
            "provider": "FINANCE_SYNC",
            "providerAccountId": "finance-sync:t:a",
        }
        with (
            patch.object(client, "get_accounts", return_value=[existing]),
            patch.object(client, "create_account") as create,
        ):
            result = await client.ensure_account(
                name="DEGIRO Pensioen",
                currency="EUR",
                provider_account_id="finance-sync:t:a",
            )
        assert result == existing
        create.assert_not_awaited()

    async def test_current_import_response_is_normalized(
        self, client: WealthfolioClient
    ) -> None:
        client._is_authenticated = True
        response = MagicMock()
        response.json.return_value = {
            "importRunId": "run-1",
            "activities": [],
            "summary": {
                "total": 5,
                "imported": 3,
                "skipped": 1,
                "duplicates": 1,
                "success": True,
            },
        }
        with patch.object(client._client, "post", return_value=response):
            result = await client.import_activities([{"date": "2026-01-01"}])
        assert result["imported"] == 3
        assert result["skipped"] == 2
        assert result["failed"] == 0


# ═══════════════════════════════════════════════════════════════════════
# Integration with exporter
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioClientIntegration:
    """Tests for the combined export + push flow."""

    async def test_full_export_and_push(
        self, client: WealthfolioClient
    ) -> None:
        """End-to-end: authenticate, check, then import activities."""
        client._is_authenticated = True

        mock_check = MagicMock()
        mock_check.status_code = 200
        mock_check.json.return_value = {
            "imported": 0,
            "skipped": 0,
            "failed": 0,
        }

        mock_import = MagicMock()
        mock_import.status_code = 200
        mock_import.json.return_value = {
            "imported": 5,
            "skipped": 0,
            "failed": 0,
        }

        with patch.object(client._client, "post") as mock_post:
            mock_post.side_effect = [mock_check, mock_import]
            result = await client.push_activities(
                activities=[{"dummy": "data"} for _ in range(5)]
            )

        assert result["imported"] == 5
        assert result["failed"] == 0
