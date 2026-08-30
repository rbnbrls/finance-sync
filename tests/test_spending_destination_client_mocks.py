"""Local provider-to-destination client contract tests.

These tests use the real destination clients and stateful in-process mocks.
They intentionally stop before network or database deployment, but exercise
the provider transformation, native payload shape, and replay behaviour.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import RawTransaction
from finance_sync.exporter.firefly.client import (
    FireflyClient,
    FireflyClientConfig,
)
from finance_sync.exporter.firefly.transaction_mapper import (
    map_transaction as map_firefly,
)
from finance_sync.exporter.wealthfolio.client import (
    WealthfolioClient,
    WealthfolioClientConfig,
)
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    map_transaction_to_wf_row,
)
from finance_sync.exporter.ynab.client import YNABClient
from finance_sync.exporter.ynab.config import YNABConfig
from finance_sync.exporter.ynab.transaction_mapper import (
    map_transaction as map_ynab,
)


class _Provider(Connector):
    @property
    def name(self) -> str:
        return "mock-provider"

    async def authenticate(self) -> None:
        return None

    async def fetch_accounts(self) -> list[Any]:
        return []

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        return []


def _canonical() -> SimpleNamespace:
    raw = RawTransaction(
        external_transaction_id="mock-payment-1",
        external_account_id="mock-account-1",
        amount=Decimal("-12.34"),
        currency_code="EUR",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        transaction_type="payment",
        status="booked",
        merchant_name="Mock Shop",
        merchant_category_code="5411",
    )
    value = _Provider.transform_transactions(
        _Provider.__new__(_Provider), [raw]
    )[0]
    return SimpleNamespace(
        id="canonical-mock-1",
        provider_key="mock-provider",
        external_transaction_id=value.external_transaction_id,
        account_id="canonical-account-1",
        occurred_at=value.occurred_at,
        amount=value.amount,
        currency_code=value.currency_code,
        status=value.status,
        transaction_type=value.transaction_type,
        description="Mock Shop purchase",
        merchant_name=value.merchant_name,
        merchant_category_code=value.merchant_category_code,
        counterparty_name=None,
        counterparty_account_reference=None,
        splits=None,
        quantity=None,
        unit_price=None,
        fee_amount=None,
        fee_currency_code=None,
        amount_in_base=None,
        base_currency_code=None,
        fx_rate=None,
        security_id=None,
        provider_fingerprint=None,
        booked_at=value.booked_at,
        revision=1,
    )


@pytest.mark.asyncio
async def test_ynab_native_client_mock_is_replay_safe() -> None:
    seen: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(200, json={"data": {"accounts": []}})
        body = request.content.decode()
        import_id = "finance-sync:mock-provider:mock-payment-1"
        if import_id in seen:
            return httpx.Response(200, json={"data": {"transaction_ids": []}})
        seen.add(import_id)
        assert import_id in body
        return httpx.Response(
            200, json={"data": {"transaction_ids": ["ynab-1"]}}
        )

    config = YNABConfig("token", "budget-1")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=config.api_base_url
    )
    transaction = _canonical()
    payload = map_ynab(transaction, account_id="ynab-account")
    async with YNABClient(config, http_client=client) as adapter:
        first = await adapter.import_transactions([payload])
        second = await adapter.import_transactions([payload])
    await client.aclose()

    assert first["data"]["transaction_ids"] == ["ynab-1"]
    assert second["data"]["transaction_ids"] == []


@pytest.mark.asyncio
async def test_firefly_native_client_mock_preserves_category_and_replay_key() -> (
    None
):
    stored: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/categories"):
            return httpx.Response(200, json={"data": []})
        if request.method == "GET" and path.endswith("/tags"):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path.endswith("/categories"):
            return httpx.Response(200, json={"data": {"id": "cat-1"}})
        if request.method == "POST" and path.endswith("/tags"):
            return httpx.Response(200, json={"data": {"id": "tag-1"}})
        body = request.content.decode()
        key = "mock-payment-1"
        stored.setdefault(key, body)
        return httpx.Response(200, json={"data": {"id": "firefly-1"}})

    transaction = _canonical()
    payload = map_firefly(transaction, account_name="Checking")
    async with FireflyClient(
        FireflyClientConfig("http://firefly.test", "token", retry_base_delay=0),
        transport=httpx.MockTransport(handler),
    ) as adapter:
        await adapter.ensure_category(payload["category_name"])
        await adapter.ensure_tag("finance-sync")
        await adapter.store_transaction(payload)
        await adapter.store_transaction(payload)

    assert "category_name" in stored["mock-payment-1"]
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_wealthfolio_native_client_mock_replays_by_idempotency_key() -> (
    None
):
    imports: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/login"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/activities/import/check"):
            body = json.loads(request.content)
            return httpx.Response(200, json=body["activities"])
        if path.endswith("/activities/import"):
            activities = json.loads(request.content)["activities"]
            imported = 0
            for activity in activities:
                key = str(activity["idempotencyKey"])
                if key not in imports:
                    imports[key] = 1
                    imported += 1
            return httpx.Response(
                200,
                json={
                    "summary": {
                        "imported": imported,
                        "duplicates": len(activities) - imported,
                    }
                },
            )
        return httpx.Response(200, json={})

    transaction = _canonical()
    row = map_transaction_to_wf_row(transaction)
    row["accountId"] = "wealthfolio-account"
    config = WealthfolioClientConfig("http://wealthfolio.test", "password")
    async with WealthfolioClient(
        config, transport=httpx.MockTransport(handler)
    ) as adapter:
        await adapter.authenticate()
        first = await adapter.push_activities([row])
        second = await adapter.push_activities([row])

    assert first["imported"] == 1
    assert second["skipped"] == 1
    assert len(imports) == 1
