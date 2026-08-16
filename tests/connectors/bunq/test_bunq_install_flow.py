"""Tests for the bunq installation/bootstrap auth flow.

Covers the full RSA installation flow that a fresh bunq API key requires
(``/installation`` → ``/device-server`` → signed ``/session-server``), the
reuse of a persisted installation across syncs, and the default
``full_auth`` behaviour.
"""

# pyright: basic

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from finance_sync.connectors.bunq import BunqConnector
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import ConnectorConfig

SESSION_SERVER_RESPONSE: dict[str, Any] = {
    "Response": [
        {"Token": {"id": 1, "token": "session-tok-abc"}},
        {"UserPerson": {"id": 99001}},
    ]
}

INSTALLATION_RESPONSE: dict[str, Any] = {
    "Response": [
        {
            "Token": {"id": 1, "token": "install-tok-123"},
            "ServerPublicKey": {
                "server_public_key": "-----BEGIN PUBLIC KEY-----"
            },
        }
    ]
}

DEVICE_SERVER_RESPONSE: dict[str, Any] = {"Response": [{"Id": {"id": 1}}]}


class InstallFlowMockTransport(httpx.MockTransport):
    """Mock bunq API supporting the full installation flow."""

    def __init__(self) -> None:
        super().__init__(self._handler)
        self._call_log: list[dict[str, Any]] = []
        self.fail_session_with: int | None = None

    @property
    def paths(self) -> list[str]:
        return [entry["path"] for entry in self._call_log]

    @property
    def call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)

    def device_server_body(self) -> dict[str, Any]:
        for entry in self._call_log:
            if entry["path"] == "/v1/device-server":
                return json.loads(entry["body"] or b"{}")
        return {}

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self._call_log.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": request.content,
            }
        )
        path = request.url.path
        if request.method == "POST" and path == "/v1/installation":
            return httpx.Response(200, json=INSTALLATION_RESPONSE)
        if request.method == "POST" and path == "/v1/device-server":
            return httpx.Response(200, json=DEVICE_SERVER_RESPONSE)
        if request.method == "POST" and path == "/v1/session-server":
            if self.fail_session_with is not None:
                return httpx.Response(
                    self.fail_session_with,
                    json={"Error": [{"error_description": "nope"}]},
                )
            return httpx.Response(200, json=SESSION_SERVER_RESPONSE)
        msg = f"No mock handler for {request.method} {path}"
        return httpx.Response(404, json={"error": msg})


def _make_connector(
    transport: httpx.MockTransport,
    *,
    options: dict[str, Any] | None = None,
) -> BunqConnector:
    opts = {"base_url": "https://api.bunq.com/v1", **(options or {})}
    http_client = httpx.AsyncClient(
        base_url=opts["base_url"],
        transport=transport,
    )
    return BunqConnector(
        config=ConnectorConfig(
            provider_type="bunq",
            credentials={"api_key": "fresh_key_123"},
            options=opts,
        ),
        http_client=http_client,
    )


class TestFullAuthDefaults:
    """full_auth must be the default for real bunq endpoints."""

    def test_full_auth_defaults_to_true(self) -> None:
        conn = _make_connector(InstallFlowMockTransport())
        assert conn._full_auth is True

    def test_full_auth_explicit_false(self) -> None:
        conn = _make_connector(
            InstallFlowMockTransport(), options={"full_auth": False}
        )
        assert conn._full_auth is False

    def test_full_auth_explicit_true(self) -> None:
        conn = _make_connector(
            InstallFlowMockTransport(), options={"full_auth": True}
        )
        assert conn._full_auth is True


class TestInstallationFlow:
    """Full bootstrap on first authenticate + reuse afterwards."""

    @pytest.mark.asyncio
    async def test_fresh_key_runs_full_install_flow(self) -> None:
        """First auth registers installation + device, then a session."""
        transport = InstallFlowMockTransport()
        conn = _make_connector(transport)

        await conn.authenticate()

        assert len(transport.call_log) == 3
        assert transport.paths == [
            "/v1/installation",
            "/v1/device-server",
            "/v1/session-server",
        ]
        assert conn._session_token == "session-tok-abc"
        assert conn._user_id == 99001

        state = conn.get_state()
        assert "client_private_key_pem" in state
        assert "-----BEGIN PRIVATE KEY-----" in state["client_private_key_pem"]
        assert state["installation_token"] == "install-tok-123"

        # Device registration carries the device description + key secret,
        # and defaults permitted_ips to [] unless configured.
        body = transport.device_server_body()
        assert body["description"] == "finance-sync"
        assert body["secret"] == "fresh_key_123"
        assert body["permitted_ips"] == []

    @pytest.mark.asyncio
    async def test_permitted_ips_list_and_string_normalised(self) -> None:
        """permitted_ips accepts both list and comma-separated string."""
        transport = InstallFlowMockTransport()
        conn = _make_connector(
            transport,
            options={"permitted_ips": "203.0.113.1, 203.0.113.2"},
        )
        await conn.authenticate()
        assert transport.device_server_body()["permitted_ips"] == [
            "203.0.113.1",
            "203.0.113.2",
        ]

        transport2 = InstallFlowMockTransport()
        conn2 = _make_connector(
            transport2,
            options={"permitted_ips": ["203.0.113.9"]},
        )
        await conn2.authenticate()
        assert transport2.device_server_body()["permitted_ips"] == [
            "203.0.113.9"
        ]

    @pytest.mark.asyncio
    async def test_reuses_persisted_installation(self) -> None:
        """A persisted installation skips install/device and only re-sessions."""
        transport = InstallFlowMockTransport()
        conn = _make_connector(transport)

        await conn.authenticate()
        state = conn.get_state()
        transport._call_log = []

        # New connector instance on the next sync tick, with the persisted
        # state injected by the orchestrator.
        conn2 = _make_connector(transport)
        conn2.set_state(state)
        await conn2.authenticate()

        assert transport.paths == ["/v1/session-server"]
        assert conn2._session_token == "session-tok-abc"
        assert conn2._user_id == 99001

    @pytest.mark.asyncio
    async def test_rejected_session_clears_stale_state(self) -> None:
        """A permanent auth failure must drop the stale installation so the
        next run registers a fresh one."""
        transport = InstallFlowMockTransport()
        transport.fail_session_with = 403
        conn = _make_connector(transport)

        with pytest.raises(PermanentError):
            await conn.authenticate()

        assert conn.get_state() == {}

    @pytest.mark.asyncio
    async def test_persisted_state_with_revoked_installation_recovers(
        self,
    ) -> None:
        """A 403 on a *reused* installation clears state and allows a fresh
        install on the next attempt."""
        transport = InstallFlowMockTransport()
        conn = _make_connector(transport)
        await conn.authenticate()
        state = conn.get_state()
        transport._call_log = []

        # Reuse attempt fails permanently (e.g. installation revoked).
        transport.fail_session_with = 403
        conn2 = _make_connector(transport)
        conn2.set_state(state)
        with pytest.raises(PermanentError):
            await conn2.authenticate()
        assert conn2.get_state() == {}

        # Next run starts clean and re-runs the full install.
        transport.fail_session_with = None
        transport._call_log = []
        conn3 = _make_connector(transport)
        await conn3.authenticate()
        assert transport.paths == [
            "/v1/installation",
            "/v1/device-server",
            "/v1/session-server",
        ]
