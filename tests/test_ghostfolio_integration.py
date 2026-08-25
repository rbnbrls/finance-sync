import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from finance_sync.exporter.ghostfolio.client import GhostfolioClient
from finance_sync.exporter.ghostfolio.config import GhostfolioConfig
from finance_sync.exporter.ghostfolio.transaction_mapper import (
    map_holding_to_ghostfolio,
    map_transaction_to_ghostfolio,
)


class Txn:
    id = "txn-1"
    external_transaction_id = "broker-1"
    transaction_type = "purchase"
    currency_code = "EUR"
    occurred_at = datetime(2026, 8, 23, tzinfo=UTC)
    amount = Decimal("-123.45")
    quantity = Decimal(2)
    unit_price = Decimal("61.725")
    fee_amount = Decimal("0.50")
    description = ""


class Security:
    ticker = "VWCE"
    isin = "IE00BK5BQT80"


class Holding:
    account_id = "account-1"
    id = "holding-1"
    observed_at = datetime(2026, 8, 23, tzinfo=UTC)
    quantity = Decimal(10)
    price = Decimal("123.45")
    market_value = Decimal("1234.50")
    currency_code = "EUR"


def test_mapper_matches_ghostfolio_import_contract() -> None:
    activity = map_transaction_to_ghostfolio(Txn(), security=Security())
    assert activity == {
        "currency": "EUR",
        "dataSource": "YAHOO",
        "date": "2026-08-23T00:00:00+00:00",
        "fee": 0.5,
        "quantity": 2.0,
        "symbol": "VWCE",
        "type": "BUY",
        "unitPrice": 61.725,
        "comment": "finance-sync:txn-1:broker-1",
    }


def test_holding_mapper_preserves_broker_exchange_symbol() -> None:
    security = Security()
    security.ticker = "BESI:XAMS"
    activity = map_holding_to_ghostfolio(
        Holding(),
        security=security,
        data_source="MANUAL",
        ghostfolio_account_id="account-1",
    )
    assert activity["symbol"] == "BESI:XAMS"
    assert activity["quantity"] == 10.0
    assert activity["unitPrice"] == 123.45
    assert activity["type"] == "BUY"
    assert activity["accountId"] == "account-1"


@pytest.mark.asyncio
async def test_client_imports_and_skips_duplicate() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(201, json={"activities": []})
        return httpx.Response(400, json={"message": ["duplicate activity"]})

    transport = httpx.MockTransport(handler)
    config = GhostfolioConfig(
        server_url="http://ghostfolio", access_token="token"
    )
    async with GhostfolioClient(config, transport=transport) as client:
        first = await client.import_activities([{"symbol": "VWCE"}])
        second = await client.import_activities([{"symbol": "VWCE"}])
    assert first["imported"] == 1
    assert second["skipped"] == 1
    assert calls[0]["activities"][0]["symbol"] == "VWCE"
