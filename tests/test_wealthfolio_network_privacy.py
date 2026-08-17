"""Network / privacy tests for the Wealthfolio integration (backlog AC).

Proves that finance-sync never sends portfolio or credential payloads to
Wealthfolio Connect, SnapTrade or any other non-configured third party:

* Every HTTP request made by :class:`WealthfolioClient` goes to the
  configured ``base_url`` and nowhere else (recording transport).
* The exporter/connector source contains no hardcoded external hosts other
  than the configured server (documented market-data / provider requests
  are the only allowed destinations — see
  ``docs/wealthfolio-multi-device-access.md`` §Network & privacy).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from finance_sync.exporter.wealthfolio.client import (
    WealthfolioClient,
    WealthfolioClientConfig,
)

BASE_URL = "https://wealthfolio.example.test"

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class RecordingTransport(httpx.AsyncBaseTransport):
    """Records every requested URL, then delegates to a handler."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[tuple[str, str]] = []  # (method, url)

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        return await self._handler(request)


async def _fake_wealthfolio_handler(request: httpx.Request) -> httpx.Response:
    """Minimal fake Wealthfolio API: auth, accounts, activities, holdings."""
    path = request.url.path
    if path == "/api/v1/auth/status":
        return httpx.Response(
            200, json={"requiresPassword": True, "oidcEnabled": False}
        )
    if path == "/api/v1/auth/login":
        return httpx.Response(
            200, json={"authenticated": True, "expiresIn": 3600}
        )
    if path == "/api/v1/accounts":
        return httpx.Response(
            200,
            json=[
                {
                    "id": "wf-acct-1",
                    "name": "Smoke Test Brokerage",
                    "currency": "EUR",
                    "provider": "FINANCE_SYNC",
                    "providerAccountId": "finance-sync:t:acct-1",
                }
            ],
        )
    if path == "/api/v1/activities/import/check":
        return httpx.Response(200, json={"valid": True, "issues": []})
    if path == "/api/v1/activities/import":
        return httpx.Response(
            200,
            json={"summary": {"imported": 1, "skipped": 0, "success": True}},
        )
    if path == "/api/v1/snapshots/import":
        return httpx.Response(200, json={"imported": 1})
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def fake_transport() -> RecordingTransport:
    return RecordingTransport(_fake_wealthfolio_handler)


def _new_client(transport: RecordingTransport) -> WealthfolioClient:
    return WealthfolioClient(
        config=WealthfolioClientConfig(base_url=BASE_URL, password="s3cret"),
        transport=transport,
    )


async def test_client_only_talks_to_configured_base_url(
    fake_transport: RecordingTransport,
) -> None:
    """A full client session never leaves the configured origin."""
    client = _new_client(fake_transport)
    try:
        assert await client.authenticate()
        await client.get_accounts()
        await client.push_activities(
            [
                {
                    "accountId": "wf-acct-1",
                    "activityType": "DIVIDEND",
                    "symbol": "VWCE",
                    "quantity": 1,
                    "unitPrice": 100,
                    "amount": 25,
                    "currency": "EUR",
                    "date": "2026-08-01",
                }
            ]
        )
        await client.import_holdings(
            [{"symbol": "VWCE", "quantity": 5, "avgCost": 100}],
            account_id="wf-acct-1",
        )
    finally:
        await client.close()

    assert fake_transport.requests, "expected at least one request"
    for method, url in fake_transport.requests:
        assert url.startswith(BASE_URL), f"{method} {url} leaves the origin!"
        assert "/api/v1/" in url


async def test_no_credential_payloads_leave_the_origin(
    fake_transport: RecordingTransport,
) -> None:
    """The password is only ever POSTed to the configured login endpoint."""
    client = _new_client(fake_transport)
    try:
        await client.authenticate()
    finally:
        await client.close()

    for _, url in fake_transport.requests:
        assert url.startswith(BASE_URL)
    # Exactly one request carries credentials: the login POST.
    login_requests = [
        (m, u) for m, u in fake_transport.requests if "/api/v1/auth/login" in u
    ]
    assert login_requests == [("POST", f"{BASE_URL}/api/v1/auth/login")]


def test_exporter_source_has_no_hardcoded_third_party_hosts() -> None:
    """The Wealthfolio exporter/connector packages hardcode no external hosts.

    Any ``http(s)`` literal found must be the API prefix or a documented
    example of the *configured* server — never a third-party service
    (Wealthfolio Connect, SnapTrade, ...).
    """
    import re
    from pathlib import Path

    pkg_root = Path(__file__).resolve().parents[1] / "src" / "finance_sync"
    allowed = {
        "192.168.3.50:8080",  # documented example of the configured server
    }
    urls: list[str] = []
    for path in (
        sorted((pkg_root / "exporter" / "wealthfolio").rglob("*.py"))
        + sorted((pkg_root / "connectors" / "bunq").rglob("*.py"))
        + sorted((pkg_root / "connectors" / "trading212").rglob("*.py"))
    ):
        text = path.read_text()
        for match in re.finditer(r"https?://[^\s'\"`<>()]+", text):
            raw = match.group(0).rstrip(".,;)]}")
            host = raw.split("/")[2] if "://" in raw else raw
            if host not in allowed:
                urls.append(f"{path.relative_to(pkg_root)}: {raw}")
    assert urls == [], "hardcoded external URLs found:\n" + "\n".join(urls)
