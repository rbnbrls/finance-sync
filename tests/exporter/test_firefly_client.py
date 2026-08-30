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


def test_mapper_projects_native_budget_and_bill_links() -> None:
    tx = SimpleNamespace(
        id="canonical-id",
        external_transaction_id="bank-id",
        amount=Decimal("-12.50"),
        currency_code="EUR",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        description="Coffee",
        transaction_type="payment",
        status="booked",
        cashflow_suggestion={"value": "food"},
    )
    result = map_transaction(
        tx,
        account_name="Checking",
        budget_name="Monthly food",
        bill_id="bill-1",
    )
    assert result["category_name"] == "food"
    assert result["budget_name"] == "Monthly food"
    assert result["bill_id"] == "bill-1"


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
async def test_client_expands_canonical_splits_to_native_lines() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"id": "split-1"}})

    async with FireflyClient(
        FireflyClientConfig("http://firefly.test", "token"),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.store_transaction(
            {
                "type": "withdrawal",
                "date": "2026-01-02",
                "currency_code": "EUR",
                "external_id": "canonical-1",
                "canonical_splits": [
                    {"amount": "4.00", "category": "food"},
                    {"amount": "6.00", "category": "household"},
                ],
            }
        )

    body = requests[0].content.decode()
    assert '"external_id":"canonical-1:0"' in body
    assert '"external_id":"canonical-1:1"' in body
    assert '"category_name":"food"' in body


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


@pytest.mark.asyncio
async def test_client_retries_rate_limits() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"data": {"id": "ok"}})

    async with FireflyClient(
        FireflyClientConfig(
            "http://firefly.test", "token", retry_base_delay=0
        ),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.store_transaction({"type": "deposit"})

    assert attempts == 2


@pytest.mark.asyncio
async def test_client_creates_native_budget_and_bill_when_missing() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/budgets"):
            return httpx.Response(200, json={"data": {"id": "budget-1"}})
        return httpx.Response(200, json={"data": {"id": "bill-1"}})

    async with FireflyClient(
        FireflyClientConfig("http://firefly.test", "token"),
        transport=httpx.MockTransport(handler),
    ) as client:
        budget = await client.ensure_budget(
            "Groceries", currency_code="EUR"
        )
        bill = await client.ensure_bill(
            "Rent", amount_min="1000", amount_max="1000"
        )

    assert budget["id"] == "budget-1"
    assert bill["id"] == "bill-1"
    assert {request.url.path for request in requests} == {
        "/api/v1/budgets",
        "/api/v1/bills",
    }
