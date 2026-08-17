"""Live-instance contract tests for the Wealthfolio HTTP client.

These tests pin the client behaviour to the **real** responses of the
self-hosted Wealthfolio instance (http://192.168.3.50:8080, Proxmox LXC
104), recorded on 2026-08-16 via direct HTTP calls against the live API
(see ``tests/exporter/fixtures/live/`` for the recorded payloads).

The fixtures cover both the unauthenticated surface (auth status, login
failure, rejected unauthenticated access) and the **authenticated**
surface recorded with a real session cookie on 2026-08-17: the accounts
list (``accounts_auth``), the activities search for the finance-sync
smoke account (``activities_search`` — 3 activities with
``ID: smoke-txn-*`` comments, ``totalRowCount: 3``) and the holdings
list (``holdings_list`` — only the EUR cash row, matching the documented
position-materialization gap of this instance).

The push-side import response is deliberately **not** recorded: replaying
the import payload against the live instance would re-write data (the
idempotent second-run proof already lives in kanban task t_b56d009f and
PR #248).  Import payload parsing is covered by the unit-level contract
tests in ``test_wealthfolio_client.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from finance_sync.exporter.wealthfolio.client import (
    WealthfolioAuthError,
    WealthfolioClient,
    WealthfolioClientConfig,
)

FIXTURES = Path(__file__).parent / "fixtures" / "live"

# Live finance-sync smoke account (Smoke Test Brokerage, recorded 2026-08-17)
SMOKE_ACCOUNT_ID = "d70e1d85-44f8-4102-aaf9-e32f4a47a862"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded live response fixture."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def client() -> WealthfolioClient:
    config = WealthfolioClientConfig(
        base_url="http://192.168.3.50:8080",
        password="not-the-real-password",
        request_timeout=15.0,
    )
    return WealthfolioClient(config=config)


# ═══════════════════════════════════════════════════════════════════════
# Auth status (real: 200 {"requiresPassword": true, "oidcEnabled": false})
# ═══════════════════════════════════════════════════════════════════════


class TestLiveAuthStatus:
    async def test_auth_status_contract(
        self, client: WealthfolioClient
    ) -> None:
        """The live instance reports password auth, no OIDC.

        Recorded from http://192.168.3.50:8080/api/v1/auth/status on
        2026-08-16: ``{"requiresPassword": true, "oidcEnabled": false}``.
        """
        fixture = _load_fixture("auth_status")
        mock_response = MagicMock()
        mock_response.status_code = fixture["status_code"]
        mock_response.json.return_value = fixture["body"]

        with patch.object(client._client, "get") as mock_get:
            mock_get.return_value = mock_response
            status = await client.check_auth_status()

        assert status == {"requiresPassword": True, "oidcEnabled": False}
        assert status["requiresPassword"] is True
        mock_get.assert_called_once_with("/api/v1/auth/status")


# ═══════════════════════════════════════════════════════════════════════
# Login failure (real: 401 {"code": 401, "message": "Invalid password"})
# ═══════════════════════════════════════════════════════════════════════


class TestLiveLoginFailure:
    async def test_wrong_password_raises_auth_error(
        self, client: WealthfolioClient
    ) -> None:
        """Wrong password yields 401 with message 'Invalid password'.

        Recorded from http://192.168.3.50:8080/api/v1/auth/login on
        2026-08-16 with a deliberately invalid probe password.
        """
        fixture = _load_fixture("login_failure")
        mock_response = MagicMock()
        mock_response.status_code = fixture["status_code"]
        mock_response.json.return_value = fixture["body"]

        with patch.object(client._client, "post") as mock_post:
            mock_post.return_value = mock_response
            with pytest.raises(WealthfolioAuthError, match="Invalid password"):
                await client.authenticate()

        assert client.is_authenticated is False


# ═══════════════════════════════════════════════════════════════════════
# Unauthenticated API access (real: 401 {"code": 401, "message": ...})
# ═══════════════════════════════════════════════════════════════════════


class TestLiveUnauthenticatedAccess:
    async def test_accounts_without_auth_is_forbidden(
        self, client: WealthfolioClient
    ) -> None:
        """Accessing accounts without a session is rejected by the API.

        Recorded from http://192.168.3.50:8080/api/v1/accounts on
        2026-08-16 without a session cookie.
        """
        _fixture = _load_fixture("accounts_unauth")
        # The client guards locally before any HTTP call is made.
        with pytest.raises(WealthfolioAuthError, match="Not authenticated"):
            await client.get_accounts()


# ═══════════════════════════════════════════════════════════════════════
# Authenticated accounts (real: 200, 2 finance-sync accounts, 2026-08-17)
# ═══════════════════════════════════════════════════════════════════════


class TestLiveAuthenticatedAccounts:
    async def test_accounts_list_contract(
        self, client: WealthfolioClient
    ) -> None:
        """Authenticated accounts list matches the recorded live response.

        Recorded from http://192.168.3.50:8080/api/v1/accounts on
        2026-08-17 with a real session cookie: 2 SECURITIES accounts,
        both ``FINANCE_SYNC`` provider, incl. the smoke account whose
        ``providerAccountId`` carries the ``finance-sync:`` prefix.
        """
        fixture = _load_fixture("accounts_auth")
        mock_response = MagicMock()
        mock_response.status_code = fixture["status_code"]
        mock_response.json.return_value = fixture["body"]

        client._is_authenticated = True
        with patch.object(client._client, "get") as mock_get:
            mock_get.return_value = mock_response
            accounts = await client.get_accounts()

        assert len(accounts) == 2
        names = {acc["name"] for acc in accounts}
        assert names == {"Smoke Test Brokerage", "snap-test-f83d67"}
        for acc in accounts:
            assert acc["accountType"] == "SECURITIES"
            assert str(acc["provider"]).upper() == "FINANCE_SYNC"
        smoke = next(
            acc for acc in accounts if acc["name"] == "Smoke Test Brokerage"
        )
        assert smoke["id"] == SMOKE_ACCOUNT_ID
        assert str(smoke["providerAccountId"]).startswith("finance-sync:")
        mock_get.assert_called_once_with("/api/v1/accounts")

    async def test_ensure_account_finds_existing_smoke_account(
        self, client: WealthfolioClient
    ) -> None:
        """``ensure_account`` matches the live providerAccountId mapping.

        The smoke account created by the exporter on 2026-08-16 is found
        by its ``finance-sync:...`` providerAccountId and returned without
        creating a duplicate (verified live in t_b56d009f / PR #248).
        """
        fixture = _load_fixture("accounts_auth")
        mock_response = MagicMock()
        mock_response.status_code = fixture["status_code"]
        mock_response.json.return_value = fixture["body"]

        client._is_authenticated = True
        with (
            patch.object(client._client, "get") as mock_get,
            patch.object(client._client, "post") as mock_post,
        ):
            mock_get.return_value = mock_response
            account = await client.ensure_account(
                name="Smoke Test Brokerage",
                currency="EUR",
                provider_account_id=(
                    "finance-sync:085231ce-564e-4cc9-a111-624618e8dec5:"
                    "22222222-2222-4222-8222-222222222222"
                ),
            )

        assert account["id"] == SMOKE_ACCOUNT_ID
        assert account["name"] == "Smoke Test Brokerage"
        # Only the GET happened — no POST create.
        mock_get.assert_called_once_with("/api/v1/accounts")
        mock_post.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Authenticated activities search (real: 200, 3 activities, totalRowCount 3)
# ═══════════════════════════════════════════════════════════════════════


class TestLiveActivitiesSearch:
    async def test_activities_search_contract(
        self, client: WealthfolioClient
    ) -> None:
        """Activities search returns the 3 recorded smoke activities.

        Recorded from http://192.168.3.50:8080/api/v1/activities/search
        on 2026-08-17 with a real session cookie: exactly 3 activities
        (BUY / SELL / DIVIDEND) in the smoke account, each carrying the
        ``ID: smoke-txn-*`` dedup comment, ``totalRowCount: 3``.
        """
        fixture = _load_fixture("activities_search")
        mock_response = MagicMock()
        mock_response.status_code = fixture["status_code"]
        mock_response.json.return_value = fixture["body"]

        client._is_authenticated = True
        with patch.object(client._client, "post") as mock_post:
            mock_post.return_value = mock_response
            result = await client.search_activities(SMOKE_ACCOUNT_ID)

        assert result["meta"]["totalRowCount"] == 3
        data = result["data"]
        assert len(data) == 3
        activity_types = {row["activityType"] for row in data}
        assert activity_types == {"BUY", "SELL", "DIVIDEND"}
        for row in data:
            assert row["accountId"] == SMOKE_ACCOUNT_ID
            assert "ID: smoke-txn-" in row["comment"]
            assert row["status"] == "POSTED"
        mock_post.assert_called_once_with(
            "/api/v1/activities/search",
            json={
                "page": 0,
                "pageSize": 1000,
                "accountIdFilter": SMOKE_ACCOUNT_ID,
            },
        )


# ═══════════════════════════════════════════════════════════════════════
# Authenticated holdings list (real: 200, only EUR cash row, 2026-08-17)
# ═══════════════════════════════════════════════════════════════════════


class TestLiveHoldingsList:
    async def test_holdings_list_contract(
        self, client: WealthfolioClient
    ) -> None:
        """Holdings list matches the recorded live response (cash only).

        Recorded from http://192.168.3.50:8080/api/v1/holdings/list on
        2026-08-17 with a real session cookie: the smoke account exposes
        only the EUR cash row (525.00) — security positions never
        materialize on this instance (documented Wealthfolio-side gap,
        t_b56d009f / t_991b5fb5).
        """
        fixture = _load_fixture("holdings_list")
        mock_response = MagicMock()
        mock_response.status_code = fixture["status_code"]
        mock_response.json.return_value = fixture["body"]

        client._is_authenticated = True
        with patch.object(client._client, "get") as mock_get:
            mock_get.return_value = mock_response
            holdings = await client.get_holdings(SMOKE_ACCOUNT_ID)

        assert len(holdings) == 1
        cash = holdings[0]
        assert cash["holdingType"] == "cash"
        assert cash["instrument"]["symbol"] == "EUR"
        assert cash["quantity"] == 525.0
        mock_get.assert_called_once_with(
            "/api/v1/holdings/list", params={"accountId": SMOKE_ACCOUNT_ID}
        )
