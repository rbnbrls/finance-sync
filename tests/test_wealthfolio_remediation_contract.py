"""Contract tests for canonical Wealthfolio repair identity and windows."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finance_sync.reconciliation.remediation.price_history import (
    HistoricalPriceStrategy,
    normalize_window,
)
from finance_sync.reconciliation.remediation.wealthfolio import (
    WealthfolioHistoricalPriceStrategy,
)
from finance_sync.services.wealthfolio_health_bridge import (
    normalize_health_issues,
    repair_capability_is_supported,
    resolve_canonical_health_issues,
)


class _Result:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> list[object]:
        return self._rows


@pytest.mark.asyncio
async def test_asset_only_finding_hydrates_canonical_identifier() -> None:
    security = SimpleNamespace(
        id="canonical-a", isin="IE00TEST", ticker="TEST", figi=None, cusip=None
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_Result([security]))
    )
    finding = normalize_health_issues(
        {
            "issues": [
                {
                    "code": "MISSING_QUOTE",
                    "affectedItems": [
                        {"assetId": "wf-asset-a", "securityId": "canonical-a"}
                    ],
                }
            ]
        },
        tenant_id="tenant-1",
        target_id="target-1",
    )

    [resolved] = await resolve_canonical_health_issues(session, finding)

    assert resolved.context["remote_entity_id"] == "wf-asset-a"
    assert resolved.context["security_id"] == "canonical-a"
    assert resolved.context["identifier"] == "IE00TEST"
    assert resolved.context["identifier_type"] == "isin"
    assert resolved.context["identity_resolution"] == "canonical"
    assert resolved.remediation_strategy == "wealthfolio_quote"


@pytest.mark.asyncio
async def test_unresolved_or_ambiguous_identity_is_manual_only() -> None:
    session = SimpleNamespace(execute=AsyncMock(return_value=_Result([])))
    finding = normalize_health_issues(
        {
            "issues": [
                {
                    "code": "MISSING_QUOTE",
                    "affectedItems": [
                        {"assetId": "wf-unknown", "ticker": "DUP"}
                    ],
                }
            ]
        },
        tenant_id="tenant-1",
        target_id="target-1",
    )

    [resolved] = await resolve_canonical_health_issues(session, finding)

    assert resolved.remediation_strategy == "unsupported"
    assert resolved.context["manual_review"] is True
    assert resolved.context["identity_resolution"] == "unresolved_or_ambiguous"


def test_identifier_types_are_preserved_including_provider_symbol() -> None:
    findings = normalize_health_issues(
        {
            "issues": [
                {
                    "code": "MISSING_QUOTE",
                    "affectedItems": [
                        {"assetId": "isin", "isin": "IE00TEST"},
                        {"assetId": "ticker", "ticker": "TEST"},
                        {"assetId": "provider", "providerSymbol": "TEST:XAMS"},
                    ],
                }
            ]
        },
        tenant_id="tenant-1",
        target_id="target-1",
    )

    assert [item.context["identifier_type"] for item in findings] == [
        "isin",
        "ticker",
        "provider_symbol",
    ]


def test_a3_capability_gate_fails_closed_for_missing_or_unsupported_target() -> (
    None
):
    health = {"issues": []}
    assert not repair_capability_is_supported(health, None)
    assert not repair_capability_is_supported(
        health,
        {
            "version": "0.1",
            "capabilities": {"quote_history_read": True, "quote_upsert": False},
        },
    )
    assert repair_capability_is_supported(
        health,
        {
            "version": "2.0",
            "capabilities": {"quote_history_read": True, "quote_upsert": True},
        },
    )


def test_date_only_end_is_next_day_and_datetime_end_is_not_shifted() -> None:
    start, end = normalize_window("2026-01-01", "2026-01-31")
    assert start == datetime(2026, 1, 1, tzinfo=UTC)
    assert end == datetime(2026, 2, 1, tzinfo=UTC)

    _, instant_end = normalize_window(
        "2026-01-01T00:00:00+00:00", "2026-01-31T12:00:00+00:00"
    )
    assert instant_end == datetime(2026, 1, 31, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_invalid_historical_window_does_not_call_gateway() -> None:
    strategy = object.__new__(HistoricalPriceStrategy)
    strategy.gateway = SimpleNamespace(get_historical_prices=AsyncMock())
    item = SimpleNamespace(
        context={
            "security_id": "sec-1",
            "identifier": "TEST",
            "start_date": "2026-02-01",
            "end_date": "2026-01-01",
        }
    )

    with pytest.raises(ValueError, match="valid half-open window"):
        await strategy.execute(item)

    strategy.gateway.get_historical_prices.assert_not_awaited()


@pytest.mark.asyncio
async def test_historical_verification_uses_exclusive_end_predicate() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=1))
    strategy = object.__new__(HistoricalPriceStrategy)
    strategy.session = session
    item = SimpleNamespace(
        tenant_id=None,
        context={
            "security_id": "sec-1",
            "interval": "1d",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
    )

    result = await strategy.verify(item)

    assert result.resolved is True
    statement = session.scalar.await_args.args[0]
    assert "security_prices.timestamp <" in str(statement)
    start, end = normalize_window("2026-01-01", "2026-01-31")
    assert start is not None
    assert end is not None
    assert start < end
    assert datetime(2026, 2, 1, tzinfo=UTC) >= end
    assert datetime(2026, 1, 31, 23, 59, tzinfo=UTC) < end


@pytest.mark.asyncio
async def test_remote_historical_verification_excludes_end_boundary() -> None:
    strategy = object.__new__(WealthfolioHistoricalPriceStrategy)
    connector = SimpleNamespace(
        QUOTE_DATA_SOURCE="CUSTOM_SCRAPER:finance-sync",
        get_quote_history=AsyncMock(
            return_value=[
                {
                    "timestamp": "2026-02-01T00:00:00+00:00",
                    "dataSource": "CUSTOM_SCRAPER:finance-sync",
                },
                {
                    "timestamp": "2026-01-31T23:59:00+00:00",
                    "dataSource": "CUSTOM_SCRAPER:finance-sync",
                },
            ]
        ),
    )
    item = SimpleNamespace(
        context={
            "remote_entity_id": "wf-asset",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        }
    )

    result = await strategy.verify(item, connector)

    assert result.resolved is True
    assert "found 1" in result.reason
