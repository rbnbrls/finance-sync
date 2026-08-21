"""Regression tests for Release 5 stage boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finance_sync.connectors.models import CanonicalAccountData
from finance_sync.services.read.prices import fetch_latest_daily_prices
from finance_sync.sync.context import SyncContext
from finance_sync.sync.stages.accounts import AccountSyncStage


def test_sync_context_is_immutable() -> None:
    context = SyncContext(
        tenant_id="tenant",
        provider_type="mock",
        since=datetime.now(UTC),
    )
    with pytest.raises(AttributeError):
        context.tenant_id = "other"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_account_stage_filters_and_does_not_commit() -> None:
    account = CanonicalAccountData(
        provider_key="mock",
        external_account_id="account-1",
        name="Checking",
        account_type="checking",
        currency_code="EUR",
    )
    writer = SimpleNamespace(persist_account=AsyncMock())

    class FakeConnector:
        supported_resources = frozenset({"holdings"})

        _rate_limited_fetch_accounts = AsyncMock(return_value=[object()])

        @staticmethod
        def transform_accounts(
            _raw: list[object],
        ) -> list[CanonicalAccountData]:
            return [account]

    connector = FakeConnector()
    result = await AccountSyncStage(writer).run(
        object(),  # type: ignore[arg-type]
        connector,  # type: ignore[arg-type]
        selected_accounts=["account-1"],
    )

    assert result.accounts == [account]
    assert result.supports_holdings is True
    writer.persist_account.assert_awaited_once()


@pytest.mark.asyncio
async def test_latest_prices_have_one_query_budget() -> None:
    scalars = SimpleNamespace(all=list)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalars))
    )

    assert (
        await fetch_latest_daily_prices(session, ["security-1", "security-2"])
        == {}
    )
    session.execute.assert_awaited_once()
