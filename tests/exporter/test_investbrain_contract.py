from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from finance_sync.exporter.investbrain.client import InvestBrainClient
from finance_sync.exporter.investbrain.config import InvestBrainConfig
from finance_sync.exporter.investbrain.transaction_mapper import (
    map_transaction_to_investbrain,
)


def _config() -> InvestBrainConfig:
    return InvestBrainConfig(
        server_url="http://investbrain.test", access_token="token"
    )


@pytest.mark.asyncio
async def test_client_lists_paginated_portfolios() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = request.url.params.get("page")
        return httpx.Response(
            200,
            json={
                "data": [{"id": page, "title": f"p{page}"}],
                "meta": {"last_page": 2},
            },
        )

    async with InvestBrainClient(
        _config(), transport=httpx.MockTransport(handler)
    ) as client:
        portfolios = await client.list_portfolios()

    assert [p["id"] for p in portfolios] == ["1", "2"]
    assert requests[0].headers["authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_client_skips_matching_business_fingerprint() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(201, json={"id": "new"})

    payload = {
        "symbol": "AAPL",
        "portfolio_id": "p1",
        "transaction_type": "BUY",
        "quantity": 2,
        "cost_basis": 100,
        "sale_price": None,
        "date": "2026-01-02",
    }
    async with InvestBrainClient(
        _config(), transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.upsert_transaction(payload, [payload.copy()])

    assert result == "skipped"
    assert calls == []


def test_mapper_uses_unit_price_and_skips_non_trades() -> None:
    txn = SimpleNamespace(
        transaction_type="purchase",
        quantity=2,
        unit_price=None,
        amount=-200,
        currency_code="USD",
        occurred_at=datetime(2026, 1, 2, 10, tzinfo=UTC),
    )
    security = SimpleNamespace(ticker="aapl", isin=None)

    mapped = map_transaction_to_investbrain(
        txn, portfolio_id="p1", security=security
    )
    assert mapped == {
        "symbol": "AAPL",
        "portfolio_id": "p1",
        "transaction_type": "BUY",
        "quantity": 2.0,
        "currency": "USD",
        "date": "2026-01-02",
        "cost_basis": 100.0,
        "sale_price": None,
        "split": False,
        "reinvested_dividend": False,
    }

    assert (
        map_transaction_to_investbrain(
            SimpleNamespace(transaction_type="dividend"),
            portfolio_id="p1",
            security=security,
        )
        is None
    )
