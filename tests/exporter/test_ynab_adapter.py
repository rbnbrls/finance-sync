from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from finance_sync.exporter.ynab.client import YNABClient
from finance_sync.exporter.ynab.config import YNABConfig
from finance_sync.exporter.ynab.transaction_mapper import map_transaction


def _transaction() -> SimpleNamespace:
    return SimpleNamespace(
        id="local-1",
        provider_key="bunq",
        external_transaction_id="payment-1",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        amount=Decimal("-12.34"),
        currency_code="EUR",
        status="booked",
        merchant_name="Example Shop",
        description="Example Shop purchase",
        transaction_type="payment",
        counterparty_account_reference=None,
        splits=None,
    )


def test_ynab_mapper_preserves_native_semantics() -> None:
    payload = map_transaction(
        _transaction(), account_id="ynab-account", category_id="cat-groceries"
    )
    assert payload["amount"] == -12340
    assert payload["payee_name"] == "Example Shop"
    assert payload["category_id"] == "cat-groceries"
    assert payload["cleared"] == "cleared"
    assert payload["import_id"] == "finance-sync:bunq:payment-1"


@pytest.mark.asyncio
async def test_ynab_client_reads_and_imports_without_leaking_token() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": {"accounts": [{"id": "a-1"}]}},
            )
        return httpx.Response(200, json={"data": {"transaction_ids": ["t-1"]}})

    transport = httpx.MockTransport(handler)
    config = YNABConfig(access_token="secret-token", budget_id="budget-1")
    http_client = httpx.AsyncClient(
        transport=transport, base_url=config.api_base_url
    )
    async with YNABClient(config, http_client=http_client) as client:
        accounts = await client.get_accounts()
        response = await client.import_transactions([{"import_id": "id-1"}])
    await http_client.aclose()

    assert accounts == [{"id": "a-1"}]
    assert response["data"]["transaction_ids"] == ["t-1"]
    assert requests[0].url.path.endswith("/budgets/budget-1/accounts")
    assert requests[0].headers["authorization"] == "Bearer secret-token"
    assert "secret-token" not in str(response)


@pytest.mark.asyncio
async def test_ynab_client_retries_rate_limits() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"data": {"accounts": []}})

    config = YNABConfig(
        access_token="token",
        budget_id="budget-1",
        retry_base_delay=0,
    )
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.api_base_url,
    )
    async with YNABClient(
        config,
        http_client=http_client,
    ) as client:
        assert await client.get_accounts() == []
    await http_client.aclose()

    assert attempts == 2
