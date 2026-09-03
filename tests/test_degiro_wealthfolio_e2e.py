"""Golden DEGIRO -> canonical -> Wealthfolio contract test."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from finance_sync.connectors.degiro_pension import DegiroPensionConnector
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.exporter.wealthfolio.exporter import (
    _holdings_quantity_corrections,
    _holdings_snapshot_payload,
    _manual_holdings_payload,
    _reconcile_holdings,
    _wf_row_to_api_activity,
)
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    map_holding_to_wf_row,
    map_transaction_to_wf_row,
)
from finance_sync.models import Account, Holding, Security, Transaction

FIXTURES = Path(__file__).parent / "connectors/degiro_pension/fixtures"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_degiro_exports_reach_exact_wealthfolio_payload() -> None:
    connector = DegiroPensionConnector(
        ConnectorConfig(
            provider_type="degiro_pension",
            options={
                "export_paths": [
                    str(FIXTURES / "transactions_nl.csv"),
                    str(FIXTURES / "account_statement_en.csv"),
                    str(FIXTURES / "portfolio_nl.csv"),
                ],
                "account_key": "e2e-pension",
                "snapshot_at": "2026-02-20",
            },
        )
    )
    await connector.authenticate()
    raw_account = (await connector.fetch_accounts())[0]
    raw_transactions = await connector.fetch_transactions(
        datetime.min.replace(tzinfo=UTC)
    )
    raw_holdings = await connector.fetch_holdings()

    account_id = str(uuid4())
    account = Account(
        id=account_id,
        tenant_id=TENANT_ID,
        provider_key="degiro_pension",
        external_account_id=raw_account.external_account_id,
        name=raw_account.name,
        account_type="investment",
        currency_code=raw_account.currency_code,
        current_balance=raw_account.current_balance,
        available_balance=raw_account.available_balance,
        is_active=True,
    )
    securities: dict[str, Security] = {}
    for item in [*raw_transactions, *raw_holdings]:
        reference = item.security_reference
        if reference and reference.isin and reference.isin not in securities:
            securities[reference.isin] = Security(
                id=str(uuid4()),
                isin=reference.isin,
                ticker=None,
                name=reference.name or reference.isin,
                security_type="etf",
                currency_code=reference.currency_code or "EUR",
            )

    activities: list[dict[str, object]] = []
    canonical_transactions: list[Transaction] = []
    for raw in raw_transactions:
        security = (
            securities.get(raw.security_reference.isin)
            if raw.security_reference and raw.security_reference.isin
            else None
        )
        transaction = Transaction(
            id=str(uuid4()),
            tenant_id=TENANT_ID,
            provider_key="degiro_pension",
            external_transaction_id=raw.external_transaction_id,
            account_id=account_id,
            security_id=security.id if security else None,
            amount=raw.amount,
            currency_code=raw.currency_code,
            amount_in_base=raw.amount_in_base,
            base_currency_code=raw.base_currency_code,
            fx_rate=raw.fx_rate,
            quantity=raw.quantity,
            unit_price=raw.unit_price,
            fee_amount=raw.fee_amount,
            fee_currency_code=raw.fee_currency_code,
            occurred_at=raw.occurred_at,
            booked_at=raw.booked_at,
            transaction_type=raw.transaction_type or "other",
            description=raw.description,
            status="booked",
            provider_fingerprint=raw.provider_fingerprint,
            revision=1,
        )
        canonical_transactions.append(transaction)
        activities.append(
            _wf_row_to_api_activity(
                map_transaction_to_wf_row(transaction, security=security),
                account_id="wf-degiro-pension",
            )
        )

    assert {row["activityType"] for row in activities} == {
        "BUY",
        "SELL",
        "DEPOSIT",
        "DIVIDEND",
        "TAX",
        "FEE",
        "INTEREST",
    }
    assert all(row["accountId"] == "wf-degiro-pension" for row in activities)
    assert len(activities) == 8
    tax = next(row for row in activities if row["activityType"] == "TAX")
    dividend = next(
        row for row in activities if row["activityType"] == "DIVIDEND"
    )
    assert tax["amount"] == pytest.approx(1.88)
    assert dividend["amount"] == pytest.approx(12.50)
    usd_sale = next(row for row in activities if row["activityType"] == "SELL")
    assert usd_sale["symbol"] == "US0378331005"
    assert usd_sale["currency"] == "USD"
    assert usd_sale["quantity"] == pytest.approx(2)
    assert usd_sale["unitPrice"] == pytest.approx(210)
    assert usd_sale["fee"] == pytest.approx(1.58)
    assert usd_sale["fxRate"] == pytest.approx(1.05)

    canonical_holdings: list[Holding] = []
    holding_rows: list[dict[str, object]] = []
    for raw in raw_holdings:
        security = securities[raw.security_reference.isin or ""]
        holding = Holding(
            id=str(uuid4()),
            tenant_id=TENANT_ID,
            account_id=account_id,
            security_id=security.id,
            observed_at=raw.observed_at,
            quantity=raw.quantity,
            cost_basis=raw.cost_basis,
            cost_basis_currency=raw.cost_basis_currency,
            market_value=raw.market_value,
            currency_code=raw.currency_code,
            price=raw.price,
            price_currency=raw.price_currency,
            source="provider_sync",
        )
        canonical_holdings.append(holding)
        holding_rows.append(map_holding_to_wf_row(holding, security=security))

    snapshot = _holdings_snapshot_payload(
        holding_rows,
        cash_balance=account.available_balance,
        cash_currency=account.currency_code,
    )
    assert len(snapshot["positions"]) == 2
    assert snapshot["cashBalances"] == {"EUR": "20.95"}
    manual_rows = _manual_holdings_payload(snapshot)
    assert all("averageCost" in row for row in manual_rows)
    assert all(row["averageCost"] is not None for row in manual_rows)

    remote_rows = [
        {
            "instrument": {"isin": row["symbol"]},
            "quantity": row["quantity"],
            "marketValue": {
                "base": str(raw.market_value),
            },
        }
        for row, raw in zip(holding_rows, raw_holdings, strict=True)
    ]
    remote_rows.append(
        {
            "instrument": None,
            "quantity": "20.95",
            "marketValue": {"base": "20.95"},
        }
    )
    assert not _reconcile_holdings(
        account=account,
        source_rows=holding_rows,
        remote_rows=remote_rows,
        absolute_tolerance=Decimal("0.01"),
        percentage_tolerance=Decimal("0.0001"),
    )


def test_reconcile_ignores_remote_cash_row() -> None:
    """Remote cash holdings must not count as positions outside source.

    The live Wealthfolio instance returns a ``holdingType: "cash"`` row
    (instrument symbol = currency code, e.g. ``EUR``) alongside the
    security positions.  finance-sync tracks cash as the account balance
    (``current_balance`` / ``available_balance``), not as a holdings
    row, so the reconcile must ignore remote cash rows — otherwise every
    export reports ``Wealthfolio bevat posities buiten de bronsnapshot``.
    Recorded live on 2026-08-16: cash row with
    ``{"holdingType": "cash", "instrument": {"symbol": "EUR"}, ...}``.
    """
    account = MagicMock()
    account.id = "acct_001"
    account.name = "Brokerage"
    account.current_balance = Decimal("525.00")

    source_rows: list[dict[str, object]] = []
    remote_rows = [
        {
            "holdingType": "cash",
            "instrument": {"id": "cash:EUR", "symbol": "EUR"},
            "quantity": "525.00",
            "marketValue": {"base": "525.00"},
        }
    ]
    findings = _reconcile_holdings(
        account=account,
        source_rows=source_rows,
        remote_rows=remote_rows,
        absolute_tolerance=Decimal("1.00"),
        percentage_tolerance=Decimal("0.005"),
    )
    assert findings == []


def test_reconcile_uses_current_balance_when_available_balance_is_empty() -> (
    None
):
    """Broker snapshots use current_balance as the Saxo cash source."""
    account = MagicMock()
    account.id = "saxo-account"
    account.name = "SaxoInvestor"
    account.available_balance = None
    account.current_balance = Decimal("35867.45")
    source_rows = [
        {
            "symbol": "AGN:XAMS",
            "quantity": "428",
            "snapshotPrice": "7.953995327102803",
        }
    ]
    remote_rows = [
        {
            "holdingType": "security",
            "instrument": {"symbol": "AGN:XAMS"},
            "quantity": 428,
            "marketValue": {"base": "3404.31"},
        },
        {
            "holdingType": "cash",
            "instrument": {"id": "cash:EUR", "symbol": "EUR"},
            "marketValue": {"base": "35867.45"},
        },
    ]

    assert (
        _reconcile_holdings(
            account=account,
            source_rows=source_rows,
            remote_rows=remote_rows,
            absolute_tolerance=Decimal("1.00"),
            percentage_tolerance=Decimal("0.005"),
        )
        == []
    )


def test_holdings_snapshot_corrects_missing_and_stale_positions() -> None:
    """The positions snapshot is authoritative for Wealthfolio quantity."""
    corrections = _holdings_quantity_corrections(
        source_rows=[
            {
                "symbol": "AGN:XAMS",
                "isin": "BMG0112X1056",
                "quantity": "428",
                "currency": "EUR",
            }
        ],
        remote_rows=[
            {
                "quantity": 400,
                "instrument": {
                    "id": "asset-agn",
                    "symbol": "AGN",
                    "isin": "BMG0112X1056",
                    "instrumentType": "EQUITY",
                },
            },
            {
                "quantity": 550,
                "instrument": {
                    "id": "asset-tkwy",
                    "symbol": "TKWY",
                    "isin": "NL0012015705",
                    "instrumentType": "EQUITY",
                },
            },
        ],
        account_id="wf-account",
        account_currency="EUR",
        tenant_id=TENANT_ID,
        finance_sync_account_id="fs-account",
    )

    assert [
        (row["activityType"], row["quantity"], row["assetId"])
        for row in corrections
    ] == [("BUY", 28.0, "asset-agn"), ("SELL", 550.0, "asset-tkwy")]
    assert all(row["unitPrice"] == 0.0 for row in corrections)
    assert all("TARGET:" in row["comment"] for row in corrections)


def test_holdings_snapshot_allows_wealthfolio_two_decimal_quantity_rounding() -> (
    None
):
    corrections = _holdings_quantity_corrections(
        source_rows=[
            {
                "symbol": "FRSHFIF.MFU",
                "quantity": "16.0227",
                "currency": "EUR",
            }
        ],
        remote_rows=[{"quantity": 16.02, "instrument": {"symbol": "FRSHFIF"}}],
        account_id="wf-account",
        account_currency="EUR",
        tenant_id=TENANT_ID,
        finance_sync_account_id="fs-account",
    )

    assert corrections == []
