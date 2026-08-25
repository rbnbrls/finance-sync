from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from finance_sync.exporter.firefly.client import (
    FireflyAPIError,
    FireflyClient,
    FireflyClientConfig,
)
from finance_sync.exporter.firefly.transaction_mapper import map_transaction


def test_map_negative_transaction() -> None:
    tx = SimpleNamespace(
        id="canonical-id",
        external_transaction_id="bank-id",
        amount=Decimal("-12.50"),
        currency_code="EUR",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        description="Coffee",
        transaction_type="payment",
        status="booked",
    )
    result = map_transaction(tx, account_name="Checking")
    assert result["type"] == "withdrawal"
    assert result["source_name"] == "Checking"
    assert result["destination_name"] == "Coffee"
    assert result["external_id"] == "bank-id"


@pytest.mark.asyncio
async def test_client_sends_transaction_payload() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"id": "123"}})

    transport = httpx.MockTransport(handler)
    async with FireflyClient(
        FireflyClientConfig("http://firefly.test", "token"), transport=transport
    ) as client:
        await client.store_transaction({"type": "deposit"})

    assert requests[0].url.path == "/api/v1/transactions"
    assert requests[0].headers["authorization"] == "Bearer token"
    assert requests[0].content.find(b"error_if_duplicate_hash") >= 0


@pytest.mark.asyncio
async def test_client_raises_api_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="duplicate transaction")

    async with FireflyClient(
        FireflyClientConfig("http://firefly.test", "token"),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(FireflyAPIError, match="422"):
            await client.store_transaction({"type": "deposit"})
