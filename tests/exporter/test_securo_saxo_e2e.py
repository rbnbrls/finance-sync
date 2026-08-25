"""SaxoInvestor -> canonical holding -> Securo export contract test."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from openpyxl import Workbook

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.saxo_investor import SaxoInvestorConnector
from finance_sync.exporter.securo.config import SecuroConfig
from finance_sync.exporter.securo.exporter import SecuroExporter

if TYPE_CHECKING:
    from pathlib import Path


HEADERS = [
    "Instrument",
    "Valuta",
    "Aantal",
    "Actuele koers",
    "Huidige waarde (EUR)",
    "Kostprijs",
    "Symbool",
    "ISIN",
    "Soort belegging",
]


def _positions_file(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(HEADERS)
    sheet.append(
        [
            "Example Equity",
            "EUR",
            2,
            100.5,
            201,
            90.25,
            "EXM:xams",
            "NL0000000001",
            "Aandeel",
        ]
    )
    sheet.append(
        ["Example ETF", "USD", 3, 20, 55, 18, "ETF:xnas", "IE0000000002", "ETF"]
    )
    workbook.save(path)


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSecuroClient:
    assets_store: list[dict[str, Any]] = []

    def __init__(self, config: SecuroConfig) -> None:
        del config

    async def __aenter__(self) -> _FakeSecuroClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def login(self) -> None:
        return None

    async def accounts(self) -> list[dict[str, Any]]:
        return [{"id": "securo-account", "name": "SaxoInvestor"}]

    async def assets(self) -> list[dict[str, Any]]:
        return self.assets_store

    async def create_asset(self, payload: dict[str, Any]) -> dict[str, Any]:
        asset = {**payload, "id": f"asset-{len(self.assets_store) + 1}"}
        self.assets_store.append(asset)
        return asset

    async def update_asset(
        self, asset_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        asset = next(
            item for item in self.assets_store if item["id"] == asset_id
        )
        asset.update(payload)
        return asset

    async def add_asset_value(
        self, asset_id: str, **payload: str
    ) -> dict[str, Any]:
        asset = next(
            item for item in self.assets_store if item["id"] == asset_id
        )
        asset["latest_value"] = payload["amount"]
        return {"asset_id": asset_id, **payload}


@pytest.mark.asyncio
async def test_saxo_all_holdings_reach_securo_and_resync_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    positions = tmp_path / "Posities_23-aug-2026.xlsx"
    _positions_file(positions)
    connector = SaxoInvestorConnector(
        ConnectorConfig(
            provider_type="saxo_investor",
            options={"export_path": str(positions)},
        )
    )
    await connector.authenticate()
    raw_account = (await connector.fetch_accounts())[0]
    raw_holdings = await connector.fetch_holdings()

    account = SimpleNamespace(
        id="canonical-account",
        name=raw_account.name,
        account_type="investment",
        currency_code="EUR",
    )
    securities = [
        SimpleNamespace(
            id=f"security-{index}",
            isin=item.security_reference.isin,
            ticker=item.security_reference.ticker,
            name=item.security_reference.name,
        )
        for index, item in enumerate(raw_holdings)
    ]
    holdings = [
        (
            SimpleNamespace(
                security_id=security.id,
                account_id=account.id,
                tenant_id="tenant",
                observed_at=item.observed_at,
                quantity=item.quantity,
                cost_basis=item.cost_basis,
                market_value=item.market_value,
                currency_code=item.currency_code,
                price=item.price,
            ),
            security,
        )
        for item, security in zip(raw_holdings, securities, strict=True)
    ]

    exporter = SecuroExporter(
        lambda: _SessionContext(),  # type: ignore[arg-type]
        SecuroConfig(email="test@example.com", password="secret"),
        "tenant",
    )
    exporter._accounts = lambda account_ids: _async_value([account])  # type: ignore[method-assign]
    exporter._transactions = lambda session, account_id, since: _async_value([])  # type: ignore[method-assign]
    exporter._holdings = lambda session, account_id, since: _async_value(
        holdings
    )  # type: ignore[method-assign]
    monkeypatch.setattr(
        "finance_sync.exporter.securo.exporter.SecuroClient",
        _FakeSecuroClient,
    )
    _FakeSecuroClient.assets_store = []

    first = await exporter.run_export(
        since=datetime(2026, 8, 1, tzinfo=UTC), push=True
    )
    second = await exporter.run_export(
        since=datetime(2026, 8, 1, tzinfo=UTC), push=True
    )

    assert first.holdings_attempted == 2
    assert first.holdings_imported == 2
    assert second.holdings_attempted == 2
    assert second.holdings_imported == 0
    assert second.holdings_skipped == 2
    assert {asset["isin"] for asset in _FakeSecuroClient.assets_store} == {
        "NL0000000001",
        "IE0000000002",
    }


async def _async_value(value: Any) -> Any:
    return value
