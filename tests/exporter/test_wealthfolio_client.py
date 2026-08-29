"""Tests for the Wealthfolio HTTP API client.

Follows TDD: RED phase — write failing tests first.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import RequestError

from finance_sync.exporter.wealthfolio.client import (
    WealthfolioAuthError,
    WealthfolioClient,
    WealthfolioClientConfig,
    resolve_wealthfolio_server_url,
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

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_container_resolves_host_local_url(self, host: str) -> None:
        url = resolve_wealthfolio_server_url(
            f"http://{host}:8088", running_in_container=True
        )
        assert url == "http://host.docker.internal:8088"

    def test_host_local_url_is_unchanged_outside_container(self) -> None:
        assert (
            resolve_wealthfolio_server_url(
                "http://localhost:8088", running_in_container=False
            )
            == "http://localhost:8088"
        )

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

    async def test_ensure_account_migrates_holdings_tracking_mode(
        self, client: WealthfolioClient
    ) -> None:
        """Existing connector accounts must calculate positions from trades."""
        client._is_authenticated = True
        existing = {
            "id": "wf-brokerage",
            "name": "Brokerage",
            "provider": "FINANCE_SYNC",
            "providerAccountId": "finance-sync:t:b",
            "trackingMode": "HOLDINGS",
        }
        migrated = {**existing, "trackingMode": "TRANSACTIONS"}
        with (
            patch.object(client, "get_accounts", return_value=[existing]),
            patch.object(
                client,
                "update_account_tracking_mode",
                return_value=migrated,
            ) as update,
        ):
            result = await client.ensure_account(
                name="Brokerage",
                currency="EUR",
                provider_account_id="finance-sync:t:b",
            )

        update.assert_awaited_once_with(existing, "TRANSACTIONS")
        assert result["trackingMode"] == "TRANSACTIONS"

    async def test_delete_accounts_keeps_only_exact_finance_sync_dataset(
        self, client: WealthfolioClient
    ) -> None:
        client._is_authenticated = True
        accounts = [
            {
                "id": "owned",
                "provider": "FINANCE_SYNC",
                "providerAccountId": "finance-sync:tenant:acct-1",
            },
            {
                "id": "old-smoke",
                "provider": "FINANCE_SYNC",
                "providerAccountId": "test:snap-test-f83d67",
            },
            {"id": "manual", "provider": "MANUAL", "providerAccountId": None},
        ]
        with (
            patch.object(client, "get_accounts", return_value=accounts),
            patch.object(client, "delete_account", new_callable=AsyncMock) as delete,
        ):
            removed = await client.delete_accounts_not_owned_by_finance_sync(
                {"finance-sync:tenant:acct-1"}
            )

        assert removed == 2
        assert [call.args[0] for call in delete.await_args_list] == [
            "old-smoke",
            "manual",
        ]

    async def test_delete_activities_not_in_source_dataset(
        self, client: WealthfolioClient
    ) -> None:
        """Stale/manual activities are removed from the projection account."""
        client._is_authenticated = True
        with (
            patch.object(
                client,
                "search_activities",
                return_value={
                    "data": [
                        {"id": "keep", "comment": "Buy | ID: current"},
                        {"id": "stale", "comment": "Old import"},
                    ]
                },
            ),
            patch.object(client, "delete_activity") as delete,
        ):
            removed = await client.delete_activities_not_in(
                "wf-account", {"current"}
            )

        assert removed == 1
        delete.assert_awaited_once_with("stale")

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
# HTTP 408 retry (server-side WF_REQUEST_TIMEOUT_MS cap)
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioClient408Retry:
    """Slow snapshot/holdings POSTs are retried with backoff on HTTP 408."""

    async def test_save_manual_holdings_retries_408_then_succeeds(
        self, client: WealthfolioClient
    ) -> None:
        """POST /snapshots retries after a 408 and succeeds on the retry."""
        client._is_authenticated = True
        timeout = MagicMock()
        timeout.status_code = 408
        timeout.raise_for_status.side_effect = Exception("408")
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"ok": True}
        ok2 = MagicMock()
        ok2.status_code = 200
        ok2.json.return_value = {"ok": True}

        with (
            # First _post_with_408_retry: 408 then 200; re-save POST: 200.
            patch.object(
                client._client, "post", side_effect=[timeout, ok, ok2]
            ) as p,
            patch.object(client, "get_assets", return_value=[]),
            patch.object(client, "get_quote_history", return_value=[]),
            patch("asyncio.sleep", new_callable=lambda: AsyncMock()) as sleep,
        ):
            result = await client.save_manual_holdings(
                [{"symbol": "AAPL", "quantity": 10, "unitPrice": 100}],
                "acct-1",
                snapshot_date="2026-08-29",
            )

        # 3 POSTs: 408 -> backoff -> 200 (snapshot), then 200 (re-save).
        assert p.await_count == 3
        sleep.assert_awaited_once()
        assert result == {"ok": True}

    async def test_save_manual_holdings_matches_exchange_qualified_symbol(
        self, client: WealthfolioClient
    ) -> None:
        """Manual quotes match when WF resolves ``VWCE.DE`` to ``VWCE``."""
        client._is_authenticated = True
        snapshot_response = MagicMock(status_code=200)
        snapshot_response.raise_for_status.return_value = None
        snapshot_response.content = b'{"ok": true}'
        quote_response = MagicMock(status_code=200)
        quote_response.raise_for_status.return_value = None

        with (
            patch.object(
                client._client, "post", return_value=snapshot_response
            ),
            patch.object(
                client,
                "get_assets",
                return_value=[{"id": "asset-vwce", "displayCode": "VWCE"}],
            ),
            patch.object(client, "get_quote_history", return_value=[]),
            patch.object(
                client._client, "put", return_value=quote_response
            ) as put,
        ):
            await client.save_manual_holdings(
                [{"symbol": "VWCE.DE", "quantity": "10", "unitPrice": "125"}],
                "acct-1",
                snapshot_date="2026-08-29",
            )

        assert put.await_count == 1
        assert put.await_args.kwargs["json"]["assetId"] == "asset-vwce"

    async def test_retry_exhausted_raises_last_408(
        self, client: WealthfolioClient
    ) -> None:
        """After retry_408_attempts 408s, the last 408 response surfaces."""
        client._is_authenticated = True
        # Default attempts = 3; make two responses 408 so the third attempt
        # is the last and its 408 raise_for_status propagates.
        timeout = MagicMock()
        timeout.status_code = 408
        timeout.raise_for_status.side_effect = Exception("408")

        with (
            patch.object(client._client, "post", return_value=timeout) as p,
            patch.object(client, "get_assets", return_value=[]),
            patch.object(client, "get_quote_history", return_value=[]),
            patch("asyncio.sleep", new_callable=lambda: AsyncMock()),
            pytest.raises(Exception, match="408"),
        ):
            await client.save_manual_holdings(
                [{"symbol": "AAPL", "quantity": 10, "unitPrice": 100}],
                "acct-1",
                snapshot_date="2026-08-29",
            )
        # 3 attempts (retry_408_attempts default), no more.
        assert p.await_count == 3

    async def test_retry_disabled_fails_fast(
        self, client_config: WealthfolioClientConfig
    ) -> None:
        """retry_408=False posts once and surfaces the 408 immediately."""
        config = WealthfolioClientConfig(
            base_url=client_config.base_url,
            password=client_config.password,
            retry_408=False,
        )
        no_retry_client = WealthfolioClient(config=config)
        no_retry_client._is_authenticated = True
        timeout = MagicMock()
        timeout.status_code = 408
        timeout.raise_for_status.side_effect = Exception("408")

        with (
            patch.object(
                no_retry_client._client, "post", return_value=timeout
            ) as p,
            patch.object(no_retry_client, "get_assets", return_value=[]),
            patch.object(no_retry_client, "get_quote_history", return_value=[]),
            patch("asyncio.sleep", new_callable=lambda: AsyncMock()) as sleep,
            pytest.raises(Exception, match="408"),
        ):
            await no_retry_client.save_manual_holdings(
                [{"symbol": "AAPL", "quantity": 10, "unitPrice": 100}],
                "acct-1",
                snapshot_date="2026-08-29",
            )
        assert p.await_count == 1
        sleep.assert_not_awaited()

    async def test_check_holdings_import_retries_408(
        self, client: WealthfolioClient
    ) -> None:
        """The holdings import-check POST also retries on 408."""
        client._is_authenticated = True
        timeout = MagicMock()
        timeout.status_code = 408
        timeout.raise_for_status.side_effect = Exception("408")
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"validationErrors": []}

        with (
            patch.object(
                client._client, "post", side_effect=[timeout, ok]
            ) as p,
            patch("asyncio.sleep", new_callable=lambda: AsyncMock()) as sleep,
        ):
            result = await client.check_holdings_import(
                [{"symbol": "AAPL", "quantity": 10}], "acct-1"
            )
        assert result == {"validationErrors": []}
        assert p.await_count == 2
        sleep.assert_awaited_once()


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

    async def test_push_activities_imports_wealthfolio_resolved_rows(
        self, client: WealthfolioClient
    ) -> None:
        """The hydrated check response must be passed to the import call."""
        client._is_authenticated = True
        activities = [{"activityType": "BUY", "symbol": "VWCE"}]
        resolved = [{**activities[0], "assetId": "asset-vwce"}]

        mock_check = MagicMock()
        mock_check.status_code = 200
        mock_check.json.return_value = resolved
        mock_import = MagicMock()
        mock_import.status_code = 200
        mock_import.json.return_value = {"imported": 1, "skipped": 0}

        with patch.object(client._client, "post") as mock_post:
            mock_post.side_effect = [mock_check, mock_import]
            await client.push_activities(activities)

        assert mock_post.call_args_list[1].kwargs == {
            "json": {"activities": resolved}
        }
