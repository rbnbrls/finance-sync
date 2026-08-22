"""Structural characterization for the read-service facade extraction."""

from pathlib import Path

import finance_sync.services.read_api as read_api
from finance_sync.services.read.accounts import AccountReadService
from finance_sync.services.read.analytics import AnalyticsReadService
from finance_sync.services.read.operational import OperationalReadService
from finance_sync.services.read.portfolio import PortfolioReadService
from finance_sync.services.read.securities import SecuritiesReadService
from finance_sync.services.read_api import ReadService

READ_API = Path(__file__).parents[1] / "src/finance_sync/services/read_api.py"


def test_read_api_is_a_small_composed_facade() -> None:
    source = READ_API.read_text(encoding="utf-8")

    assert len(source.splitlines()) <= 300
    assert "Legacy implementation retained" not in source
    assert ReadService.list_accounts is AccountReadService.list_accounts
    assert ReadService.get_portfolio is PortfolioReadService.get_portfolio
    assert ReadService.get_holdings is PortfolioReadService.get_holdings
    assert ReadService.list_securities is SecuritiesReadService.list_securities
    assert ReadService.get_net_worth is AnalyticsReadService.get_net_worth
    assert ReadService.list_transactions is OperationalReadService.list_transactions


def test_public_read_response_schemas_remain_exported() -> None:
    expected = {
        "AccountSummary",
        "TopLevelTransactionListResponse",
        "HoldingsListResponse",
        "PortfolioResponse",
        "SecurityListResponse",
        "TopLevelPriceListResponse",
        "NetWorthResponse",
        "CashflowResponse",
        "SyncRunListResponse",
    }

    assert expected.issubset(vars(read_api))
