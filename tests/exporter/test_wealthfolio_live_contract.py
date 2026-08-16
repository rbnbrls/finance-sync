"""Live-instance contract tests for the Wealthfolio HTTP client.

These tests pin the client behaviour to the **real** responses of the
self-hosted Wealthfolio instance (http://192.168.3.50:8080, Proxmox LXC
104), recorded on 2026-08-16 via direct HTTP calls against the live API
(see ``tests/exporter/fixtures/live/`` for the recorded payloads).

Only unauthenticated endpoints can be covered this way — the push target
password lives in the instance (``/root/wealthfolio.creds``) and is not
committed.  Once ``WEALTHFOLIO_PASSWORD`` is available, extend this file
with a recorded authenticated exchange (accounts list + activities
import) using the same fixture pattern.
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
