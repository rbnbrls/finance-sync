"""Multi-client consistency tests for the Wealthfolio route (backlog AC).

Verifies the end-to-end claim: ``provider -> finance-sync -> Wealthfolio ->
two clients`` — two independent client sessions (desktop + mobile PWA both
talk to the *same* self-hosted instance) must observe identical data, and
the route must *fail* when one client would see stale data.

The live equivalent is ``scripts/wealthfolio_multi_client_smoke.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from finance_sync.exporter.wealthfolio.client import (
    WealthfolioClient,
    WealthfolioClientConfig,
)
from finance_sync.monitoring.wealthfolio_monitor import check_export_freshness

BASE_URL = "https://wealthfolio.example.test"


class _SharedState:
    """Shared in-memory Wealthfolio state both clients read/write."""

    def __init__(self) -> None:
        self.accounts: list[dict[str, Any]] = [
            {
                "id": "wf-acct-1",
                "name": "Smoke Test Brokerage",
                "currency": "EUR",
                "provider": "FINANCE_SYNC",
            }
        ]
        self.activities: list[dict[str, Any]] = []


def _stateful_handler(state: _SharedState):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/status":
            return httpx.Response(
                200, json={"requiresPassword": True, "oidcEnabled": False}
            )
        if path == "/api/v1/auth/login":
            return httpx.Response(200, json={"authenticated": True})
        if path == "/api/v1/accounts":
            return httpx.Response(200, json=state.accounts)
        if path == "/api/v1/assets" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/api/v1/assets" and request.method == "POST":
            payload: dict[str, Any] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    **payload,
                    "id": "wf-asset-1",
                },
            )
        if path == "/api/v1/activities/import/check":
            return httpx.Response(200, json={"valid": True, "issues": []})
        if path == "/api/v1/activities/import":
            payload: dict[str, Any] = json.loads(request.content)
            activities = payload.get("activities", [])
            state.activities.extend(activities)
            return httpx.Response(
                200,
                json={
                    "summary": {
                        "imported": len(activities),
                        "skipped": 0,
                        "success": True,
                    }
                },
            )
        if path == "/api/v1/activities/search":
            return httpx.Response(
                200,
                json={
                    "activities": state.activities,
                    "total": len(state.activities),
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    return handler


@pytest.fixture
def shared_state() -> _SharedState:
    return _SharedState()


def _client(transport: httpx.AsyncBaseTransport) -> WealthfolioClient:
    return WealthfolioClient(
        config=WealthfolioClientConfig(base_url=BASE_URL, password="s3cret"),
        transport=transport,
    )


async def test_two_clients_observe_identical_data(
    shared_state: _SharedState,
) -> None:
    """Desktop + mobile clients see the exact same accounts and activities."""
    writer = _client(httpx.MockTransport(_stateful_handler(shared_state)))
    try:
        await writer.authenticate()
        await writer.push_activities(
            [
                {
                    "accountId": "wf-acct-1",
                    "activityType": "BUY",
                    "symbol": "VWCE",
                    "quantity": 10,
                    "unitPrice": 100,
                    "amount": -1000,
                    "currency": "EUR",
                    "date": "2026-08-10",
                }
            ]
        )
    finally:
        await writer.close()

    # Two independent reader sessions against the same instance.
    reader_a = _client(httpx.MockTransport(_stateful_handler(shared_state)))
    reader_b = _client(httpx.MockTransport(_stateful_handler(shared_state)))
    try:
        await reader_a.authenticate()
        await reader_b.authenticate()
        accounts_a = await reader_a.get_accounts()
        accounts_b = await reader_b.get_accounts()
        assert accounts_a == accounts_b
        assert accounts_a == shared_state.accounts
        # Activities visible to both.
        search_a = await reader_a.search_activities("wf-acct-1")
        search_b = await reader_b.search_activities("wf-acct-1")
        assert search_a == search_b
        assert search_a["total"] == 1
    finally:
        await reader_a.close()
        await reader_b.close()


async def test_new_data_is_visible_to_both_clients(
    shared_state: _SharedState,
) -> None:
    """A fresh import is immediately visible to a newly-opened client."""
    writer = _client(httpx.MockTransport(_stateful_handler(shared_state)))
    try:
        await writer.authenticate()
        await writer.push_activities(
            [
                {
                    "accountId": "wf-acct-1",
                    "activityType": "DIVIDEND",
                    "symbol": "VWCE",
                    "quantity": 1,
                    "unitPrice": 100,
                    "amount": 25,
                    "currency": "EUR",
                    "date": "2026-08-12",
                }
            ]
        )
    finally:
        await writer.close()

    mobile = _client(httpx.MockTransport(_stateful_handler(shared_state)))
    try:
        await mobile.authenticate()
        search = await mobile.search_activities("wf-acct-1")
        assert search["total"] == 1
        assert search["activities"][0]["activityType"] == "DIVIDEND"
    finally:
        await mobile.close()


def test_route_fails_when_one_client_sees_stale_data() -> None:
    """The freshness gate fails when data has not been delivered recently.

    Simulates the operational check that would catch a client (or the
    pipeline) serving outdated portfolio data: the newest delivery cursor
    is older than the allowed window.
    """
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    stale = check_export_freshness(
        [(now - timedelta(hours=30), "acct-1")], max_stale_hours=24, now=now
    )
    assert stale.ok is False

    fresh = check_export_freshness(
        [(now - timedelta(minutes=5), "acct-1")], max_stale_hours=24, now=now
    )
    assert fresh.ok is True
