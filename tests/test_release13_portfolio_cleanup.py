"""Characterization tests for the Release 13 portfolio cleanup."""

from pathlib import Path

from finance_sync.services.read.portfolio import PortfolioReadService
from finance_sync.services.read.schemas import (
    AccountPortfolioBreakdown,
    HoldingBreakdown,
    HoldingItemResponse,
    HoldingsListResponse,
    PortfolioResponse,
)
from finance_sync.services.read_api import ReadService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
READ_API = PROJECT_ROOT / "src/finance_sync/services/read_api.py"
PORTFOLIO = PROJECT_ROOT / "src/finance_sync/services/read/portfolio.py"


def test_portfolio_and_holdings_are_component_owned() -> None:
    facade = READ_API.read_text(encoding="utf-8")
    portfolio = PORTFOLIO.read_text(encoding="utf-8")

    assert "select(" not in facade
    assert "from finance_sync.services.read_api" not in portfolio
    assert ReadService.get_portfolio is PortfolioReadService.get_portfolio
    assert ReadService.get_holdings is PortfolioReadService.get_holdings


def test_portfolio_response_contracts_stay_in_read_schemas() -> None:
    schemas = {
        AccountPortfolioBreakdown,
        HoldingBreakdown,
        HoldingItemResponse,
        HoldingsListResponse,
        PortfolioResponse,
    }
    assert all(item.__module__.endswith("read.schemas") for item in schemas)
