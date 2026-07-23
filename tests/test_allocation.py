"""Tests for the Allocation API endpoints.

# pyright: basic

Tests cover:
- API endpoint registration (OpenAPI schema)
- Authentication guards
- AllocationService unit tests (mocked session)
- Allocation computation logic
- Multi-currency conversion (via mocked FxService)
- Per-account scoping
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db
from finance_sync.services.allocation import AllocationService

# ── Test helpers ──────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-key-at-least-16-chars"


def _assert_sql_contains(mock: AsyncMock, fragment: str) -> None:
    """Assert that the compiled SQL from execute() contains ``fragment``."""
    call_args = mock.execute.call_args
    assert call_args is not None
    stmt = str(
        call_args[0][0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert fragment in stmt


# ── Shared fixtures ───────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        access_token_expire_minutes=15,
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings=settings)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar.return_value = 0
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def svc(mock_session: AsyncMock) -> AllocationService:
    return AllocationService(mock_session)


# ── Helper to create a realistic holding mock ─────────────────────────


def _make_holding(
    security_id: str = "sec-1",
    account_id: str = "acct-1",
    market_value: str = "1000",
    currency_code: str = "EUR",
    observed_at: datetime | None = None,
) -> MagicMock:
    h = MagicMock()
    h.security_id = security_id
    h.account_id = account_id
    h.market_value = Decimal(market_value)
    h.currency_code = currency_code
    h.observed_at = observed_at or datetime(2025, 6, 15, tzinfo=UTC)
    h.quantity = Decimal(10)
    h.cost_basis = Decimal(800)
    h.cost_basis_currency = currency_code
    h.price = Decimal(100)
    h.price_currency = currency_code
    h.source = "provider_sync"
    return h


def _make_security(
    security_id: str = "sec-1",
    security_type: str = "stock",
    name: str = "Test Security",
) -> MagicMock:
    s = MagicMock()
    s.id = security_id
    s.security_type = security_type
    s.name = name
    s.ticker = "TST"
    s.currency_code = "EUR"
    s.isin = None
    s.figi = None
    return s


def _make_account(
    account_id: str = "acct-1",
    name: str = "Test Account",
    account_type: str = "brokerage",
) -> MagicMock:
    a = MagicMock()
    a.id = account_id
    a.name = name
    a.account_type = account_type
    a.currency_code = "EUR"
    return a


# ═══════════════════════════════════════════════════════════════════════
# OpenAPI schema — endpoint registered
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAPIRegistration:
    """Verify allocation endpoint appears in the OpenAPI schema."""

    def test_allocation_endpoint_registered(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/allocation" in paths
        assert paths["/api/v1/allocation"]["get"]["tags"] == ["allocation"]

    def test_allocation_response_schema(self, client: TestClient) -> None:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

        assert "AllocationResponse" in schemas
        assert "AllocationBucket" in schemas
        assert "AccountAllocationBreakdown" in schemas

        # Check AllocationResponse fields
        resp_schema = schemas["AllocationResponse"]["properties"]
        assert "by_asset_class" in resp_schema
        assert "by_sector" in resp_schema
        assert "by_region" in resp_schema
        assert "total_value" in resp_schema
        assert "currency_code" in resp_schema
        assert "accounts" in resp_schema
        assert "as_of" in resp_schema

        # Check AllocationBucket fields
        bucket_schema = schemas["AllocationBucket"]["properties"]
        assert "name" in bucket_schema
        assert "value" in bucket_schema
        assert "percentage" in bucket_schema


# ═══════════════════════════════════════════════════════════════════════
# Auth guard test
# ═══════════════════════════════════════════════════════════════════════


class TestAuthGuards:
    """Allocation endpoint requires authentication."""

    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        response: Response = client.get("/api/v1/allocation")
        assert response.status_code == 401
        assert "detail" in response.json()

    def test_bad_token_returns_401(self, client: TestClient) -> None:
        headers = {"Authorization": "Bearer invalid-token-here"}
        response: Response = client.get(
            "/api/v1/allocation", headers=headers
        )
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# Service internal edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestServiceEdgeCases:
    """Internal service edge cases for uncovered code paths."""

    async def test_empty_security_ids_returns_empty_sector_map(
        self, mock_session: AsyncMock
    ) -> None:
        """_load_sector_metadata with empty list returns {}."""
        svc = AllocationService(mock_session)
        result = await svc._load_sector_metadata([])
        assert result == {}
        mock_session.execute.assert_not_called()

    async def test_empty_security_ids_returns_empty_region_map(
        self, mock_session: AsyncMock
    ) -> None:
        """_load_region_metadata with empty list returns {}."""
        svc = AllocationService(mock_session)
        result = await svc._load_region_metadata([])
        assert result == {}
        mock_session.execute.assert_not_called()
    async def test_sector_uses_label_when_available(
        self, mock_session: AsyncMock
    ) -> None:
        """When sector metadata has a label, it's used directly."""
        sector_mock = MagicMock()
        sector_mock.security_id = "sec-1"
        sector_mock.label = "Technology"
        sector_mock.metadata_type = "sector_exposure"
        sector_mock.metadata_json = {}
        result_obj = MagicMock()
        result_obj.scalars.return_value.all.return_value = [sector_mock]
        mock_session.execute.return_value = result_obj

        svc = AllocationService(mock_session)
        result = await svc._load_sector_metadata(["sec-1"])
        assert result == {"sec-1": "Technology"}
        mock_session.execute.assert_called_once()
    async def test_region_with_industry_from_sector_exposure(
        self, mock_session: AsyncMock
    ) -> None:
        """Region from sector_exposure metadata_type with 'region' key."""
        region_mock = MagicMock()
        region_mock.security_id = "sec-1"
        region_mock.metadata_type = "sector_exposure"
        region_mock.metadata_json = {"region": "Europe"}
        result_obj = MagicMock()
        result_obj.scalars.return_value.all.return_value = [region_mock]
        mock_session.execute.return_value = result_obj

        svc = AllocationService(mock_session)
        result = await svc._load_region_metadata(["sec-1"])
        assert result == {"sec-1": "Europe"}
# ═══════════════════════════════════════════════════════════════════════


class TestAllocationServiceEmpty:
    """AllocationService with no holdings."""

    async def test_empty_tenant_returns_zero(
        self, svc: AllocationService
    ) -> None:
        result = await svc.get_allocation(tenant_id="t1")
        assert result.total_value == Decimal(0)
        assert result.by_asset_class == []
        assert result.by_sector == []
        assert result.by_region == []
        assert result.accounts == []

    async def test_empty_passes_tenant_filter(
        self, mock_session: AsyncMock
    ) -> None:
        svc = AllocationService(mock_session)
        await svc.get_allocation(tenant_id="tenant-abc")
        _assert_sql_contains(mock_session, "tenant-abc")


class TestAllocationServiceWithData:
    """AllocationService with mocked holdings."""

    async def _setup_holdings(
        self, mock_session: AsyncMock
    ) -> tuple[MagicMock, list[MagicMock], list[MagicMock]]:
        """Set up mock session with holdings, securities, and accounts.

        Holdings:
          - acct-1: sec-1 (stock, EUR 1000), sec-2 (etf, EUR 500)
          - acct-2: sec-3 (bond, EUR 300)

        Returns (mock_result, holdings, securities).
        """
        h1 = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="1000",
            observed_at=datetime(2025, 6, 15, tzinfo=UTC),
        )
        h2 = _make_holding(
            security_id="sec-2",
            account_id="acct-1",
            market_value="500",
            observed_at=datetime(2025, 6, 15, tzinfo=UTC),
        )
        h3 = _make_holding(
            security_id="sec-3",
            account_id="acct-2",
            market_value="300",
            observed_at=datetime(2025, 6, 14, tzinfo=UTC),
        )
        holdings = [h1, h2, h3]

        sec1 = _make_security(
            security_id="sec-1", security_type="stock", name="Apple"
        )
        sec2 = _make_security(
            security_id="sec-2", security_type="etf", name="VWCE"
        )
        sec3 = _make_security(
            security_id="sec-3", security_type="bond", name="Gov Bond"
        )
        securities = [sec1, sec2, sec3]

        acct1 = _make_account(
            account_id="acct-1",
            name="Brokerage Account",
            account_type="brokerage",
        )
        acct2 = _make_account(
            account_id="acct-2",
            name="Bond Account",
            account_type="investment",
        )

        # Mock the execute calls in sequence:
        # Call 1: holdings query → returns holdings
        # Call 2: accounts query → returns accounts
        # Call 3: securities query → returns securities

        holdings_result = MagicMock()
        holdings_result.scalars.return_value.all.return_value = holdings

        accounts_result = MagicMock()
        accounts_result.scalars.return_value.all.return_value = [
            acct1,
            acct2,
        ]

        sec_result_obj = MagicMock()
        sec_result_obj.scalars.return_value.all.return_value = securities

        # Metadata results (empty)
        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            holdings_result,  # latest holdings
            accounts_result,  # accounts query
            sec_result_obj,  # securities query
            empty_result,  # sector metadata (subquery)
            empty_result,  # sector metadata (main query)
            empty_result,  # region metadata (subquery)
            empty_result,  # region metadata (main query)
        ]

        return holdings_result, holdings, securities

    async def test_multiple_holdings_asset_class(
        self, mock_session: AsyncMock
    ) -> None:
        await self._setup_holdings(mock_session)
        svc = AllocationService(mock_session)

        result = await svc.get_allocation(tenant_id="t1")

        # Total: 1000 + 500 + 300 = 1800
        assert result.total_value == Decimal(1800)

        # By asset class: stock = 1000, etf = 500, bond = 300
        by_ac = {b.name: b for b in result.by_asset_class}
        assert len(by_ac) == 3
        assert by_ac["stock"].value == Decimal(1000)
        assert by_ac["stock"].percentage == Decimal("55.56")
        assert by_ac["etf"].value == Decimal(500)
        assert by_ac["etf"].percentage == Decimal("27.78")
        assert by_ac["bond"].value == Decimal(300)
        assert by_ac["bond"].percentage == Decimal("16.67")

    async def test_account_breakdown(
        self, mock_session: AsyncMock
    ) -> None:
        await self._setup_holdings(mock_session)
        svc = AllocationService(mock_session)

        result = await svc.get_allocation(tenant_id="t1")

        assert len(result.accounts) == 2

        # Find account breakdowns
        acct_map = {a.account_id: a for a in result.accounts}

        # Account 1: 1000 + 500 = 1500
        acct1 = acct_map["acct-1"]
        assert acct1.account_name == "Brokerage Account"
        assert acct1.total_value == Decimal(1500)
        ac1_ac = {b.name: b for b in acct1.by_asset_class}
        assert ac1_ac["stock"].value == Decimal(1000)
        assert ac1_ac["etf"].value == Decimal(500)

        # Account 2: 300
        acct2 = acct_map["acct-2"]
        assert acct2.account_name == "Bond Account"
        assert acct2.total_value == Decimal(300)
        ac2_ac = {b.name: b for b in acct2.by_asset_class}
        assert ac2_ac["bond"].value == Decimal(300)

    async def test_sector_and_region_unclassified(
        self, mock_session: AsyncMock
    ) -> None:
        await self._setup_holdings(mock_session)
        svc = AllocationService(mock_session)

        result = await svc.get_allocation(tenant_id="t1")

        # Without sector metadata, everything should be "Unclassified"
        assert len(result.by_sector) == 1
        assert result.by_sector[0].name == "Unclassified"
        assert result.by_sector[0].value == Decimal(1800)

        assert len(result.by_region) == 1
        assert result.by_region[0].name == "Unclassified"
        assert result.by_region[0].value == Decimal(1800)

    async def test_passes_tenant_filter(
        self, mock_session: AsyncMock
    ) -> None:
        svc = AllocationService(mock_session)

        # Set up execute to return empty holdings first (so it continues)
        holdings_result = MagicMock()
        holdings_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = holdings_result

        await svc.get_allocation(tenant_id="tenant-xyz")
        _assert_sql_contains(mock_session, "tenant-xyz")

    async def test_account_scoping(
        self, mock_session: AsyncMock
    ) -> None:
        """Test that account_id filter is passed through."""
        h1 = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="1000",
        )
        holdings = [h1]
        sec1 = _make_security(
            security_id="sec-1", security_type="stock"
        )
        acct1 = _make_account(account_id="acct-1")

        holdings_result = MagicMock()
        holdings_result.scalars.return_value.all.return_value = holdings
        accounts_result = MagicMock()
        accounts_result.scalars.return_value.all.return_value = [acct1]
        sec_result = MagicMock()
        sec_result.scalars.return_value.all.return_value = [sec1]
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            holdings_result,
            accounts_result,
            sec_result,
            empty,  # sector subquery
            empty,  # sector main
            empty,  # region subquery
            empty,  # region main
        ]

        svc = AllocationService(mock_session)
        result = await svc.get_allocation(
            tenant_id="t1", account_id="acct-1"
        )
        assert result.total_value == Decimal(1000)
        assert len(result.accounts) == 1
        assert result.accounts[0].account_id == "acct-1"


class TestAllocationServiceSingleHolding:
    """Edge cases with a single holding."""

    async def test_single_holding_asset_class(
        self, mock_session: AsyncMock
    ) -> None:
        h = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="5000",
        )
        sec = _make_security(
            security_id="sec-1", security_type="crypto"
        )
        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = [h]
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = [sec]
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            empty,
            empty,
            empty,
            empty,
        ]

        svc = AllocationService(mock_session)
        result = await svc.get_allocation(tenant_id="t1")

        assert result.total_value == Decimal(5000)
        assert len(result.by_asset_class) == 1
        assert result.by_asset_class[0].name == "crypto"
        assert result.by_asset_class[0].value == Decimal(5000)
        assert result.by_asset_class[0].percentage == Decimal(100)

    async def test_zero_market_value(
        self, mock_session: AsyncMock
    ) -> None:
        h = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="0",
        )
        sec = _make_security(security_id="sec-1")
        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = [h]
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = [sec]
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            empty,
            empty,
            empty,
            empty,
        ]

        svc = AllocationService(mock_session)
        result = await svc.get_allocation(tenant_id="t1")

        assert result.total_value == Decimal(0)
        assert len(result.by_asset_class) == 1
        assert result.by_asset_class[0].value == Decimal(0)
        assert result.by_asset_class[0].percentage == Decimal(0)


class TestAllocationPercentages:
    """Test percentage calculations."""

    async def test_percentages_sum_to_100(
        self, mock_session: AsyncMock
    ) -> None:
        """Evenly split across 4 asset classes."""
        holdings = []
        securities = []
        for sid, stype, val in (
            [
                ("sec-1", "stock", "250"),
                ("sec-2", "etf", "250"),
                ("sec-3", "bond", "250"),
                ("sec-4", "crypto", "250"),
            ]
        ):
            h = _make_holding(
                security_id=sid,
                account_id="acct-1",
                market_value=val,
            )
            holdings.append(h)
            sec = _make_security(security_id=sid, security_type=stype)
            securities.append(sec)

        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = holdings
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = securities
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            empty,
            empty,
            empty,
            empty,
        ]

        svc = AllocationService(mock_session)
        result = await svc.get_allocation(tenant_id="t1")

        assert result.total_value == Decimal(1000)
        total_pct = sum(b.percentage for b in result.by_asset_class)
        assert total_pct == Decimal(100)
        for b in result.by_asset_class:
            assert b.percentage == Decimal(25)


# ═══════════════════════════════════════════════════════════════════════
# Integration with FxService (mocked)
# ═══════════════════════════════════════════════════════════════════════


class TestAllocationFxConversion:
    """Allocation with multi-currency conversion."""

    async def test_fx_conversion_applied(
        self, mock_session: AsyncMock
    ) -> None:
        """When target_currency differs from holding currency,
        FxService is called."""
        h = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="1000",
            currency_code="USD",
        )
        sec = _make_security(security_id="sec-1", security_type="stock")
        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = [h]
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = [sec]
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            empty,
            empty,
            empty,
            empty,
        ]

        # Create mock FxService that returns a fixed conversion rate
        fx_service = AsyncMock()
        conv_result = MagicMock()
        conv_result.converted_amount = Decimal(850)
        conv_result.rate_used = Decimal("0.85")
        conv_result.rate_timestamp = datetime(2025, 6, 15, tzinfo=UTC)
        conv_result.source = "test"
        fx_service.convert.return_value = conv_result

        svc = AllocationService(mock_session, fx_service=fx_service)
        result = await svc.get_allocation(
            tenant_id="t1", target_currency="EUR"
        )

        # 1000 USD should be converted to 850 EUR at 0.85 rate
        assert result.total_value == Decimal(850)
        assert result.currency_code == "EUR"
        fx_service.convert.assert_called_once()

    async def test_no_conversion_same_currency(
        self, mock_session: AsyncMock
    ) -> None:
        """When holding currency == target_currency, no FX call."""
        h = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="1000",
            currency_code="EUR",
        )
        sec = _make_security(security_id="sec-1", security_type="stock")
        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = [h]
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = [sec]
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            empty,
            empty,
            empty,
            empty,
        ]

        fx_service = AsyncMock()
        svc = AllocationService(mock_session, fx_service=fx_service)
        result = await svc.get_allocation(
            tenant_id="t1", target_currency="EUR"
        )

        assert result.total_value == Decimal(1000)
        assert result.currency_code == "EUR"
        fx_service.convert.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Utility function tests
# ═══════════════════════════════════════════════════════════════════════


class TestRegionNormalise:
    """Unit tests for the _region_normalise mapping function."""

    def test_us_variants_map_to_north_america(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        for variant in ["US", "USA", "United States"]:
            assert _region_normalise(variant) == "North America"

    def test_common_european_countries(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        assert _region_normalise("GB") == "Europe"
        assert _region_normalise("DE") == "Europe"
        assert _region_normalise("NL") == "Europe"
        assert _region_normalise("CH") == "Europe"

    def test_asia_pacific_countries(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        assert _region_normalise("JP") == "Asia Pacific"
        assert _region_normalise("AU") == "Asia Pacific"
        assert _region_normalise("SG") == "Asia Pacific"

    def test_latin_america(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        assert _region_normalise("BR") == "Latin America"
        assert _region_normalise("MX") == "Latin America"

    def test_middle_east_africa(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        assert _region_normalise("ZA") == "Middle East & Africa"
        assert _region_normalise("AE") == "Middle East & Africa"

    def test_case_insensitive(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        assert _region_normalise("us") == "North America"
        assert _region_normalise("uk") == "Europe"
        assert _region_normalise("jp") == "Asia Pacific"

    def test_unknown_region_returns_as_is(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        assert _region_normalise("Antarctica") == "Antarctica"
        assert _region_normalise("   ").strip() == ""

    def test_trimmed_input(self) -> None:
        from finance_sync.services.allocation import _region_normalise

        assert _region_normalise("  US  ") == "North America"


class TestPickDominantCurrency:
    """Unit tests for the _pick_dominant_currency function."""

    def test_empty_list_returns_eur(self) -> None:
        from finance_sync.services.allocation import _pick_dominant_currency

        assert _pick_dominant_currency([]) == "EUR"

    def test_single_currency(self) -> None:
        from finance_sync.services.allocation import _pick_dominant_currency

        assert _pick_dominant_currency(["USD"]) == "USD"

    def test_majority_wins(self) -> None:
        from finance_sync.services.allocation import _pick_dominant_currency

        result = _pick_dominant_currency(
            ["EUR", "EUR", "USD", "EUR", "GBP"]
        )
        assert result == "EUR"

    def test_tie_returns_first_majority(self) -> None:
        from finance_sync.services.allocation import _pick_dominant_currency

        result = _pick_dominant_currency(["USD", "EUR", "EUR", "USD"])
        # Both have 2, max returns first seen with max count
        assert result in ("USD", "EUR")


class TestSectorClassification:
    """Test sector classification from SecurityMetadataObservation."""

    async def test_sector_from_metadata_json(
        self, mock_session: AsyncMock
    ) -> None:
        """When sector metadata has no label, uses metadata_json."""
        h = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="1000",
        )
        sec = _make_security(security_id="sec-1", security_type="stock")
        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = [h]
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = [sec]

        # Sector metadata without label — uses metadata_json
        sector_mock = MagicMock()
        sector_mock.security_id = "sec-1"
        sector_mock.label = None
        sector_mock.metadata_type = "sector_exposure"
        sector_mock.metadata_json = {"sector": "Technology"}
        sector_result = MagicMock()
        sector_result.scalars.return_value.all.return_value = [
            sector_mock
        ]

        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            sector_result,  # sector metadata
            empty,  # region metadata
        ]

        svc = AllocationService(mock_session)
        result = await svc.get_allocation(tenant_id="t1")

        assert len(result.by_sector) == 1
        assert result.by_sector[0].name == "Technology"
        assert result.by_sector[0].value == Decimal(1000)


class TestRegionClassification:
    """Test region classification from company profile metadata."""

    async def test_region_from_company_profile(
        self, mock_session: AsyncMock
    ) -> None:
        """Company profile metadata maps countries to regions."""
        h = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="1000",
        )
        sec = _make_security(security_id="sec-1", security_type="stock")
        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = [h]
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = [sec]

        # Company profile metadata with country -> maps to region
        profile_mock = MagicMock()
        profile_mock.security_id = "sec-1"
        profile_mock.metadata_type = "company_profile"
        profile_mock.metadata_json = {"country": "US"}
        region_result = MagicMock()
        region_result.scalars.return_value.all.return_value = [
            profile_mock
        ]

        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            empty,    # sector metadata
            region_result,  # region metadata
        ]

        svc = AllocationService(mock_session)
        result = await svc.get_allocation(tenant_id="t1")

        assert len(result.by_region) == 1
        assert result.by_region[0].name == "North America"
        assert result.by_region[0].value == Decimal(1000)


class TestFxConversionEdgeCases:
    """FX conversion edge cases."""

    async def test_fx_conversion_returns_none(
        self, mock_session: AsyncMock
    ) -> None:
        """When FxService returns None, fall back to original value."""
        h = _make_holding(
            security_id="sec-1",
            account_id="acct-1",
            market_value="1000",
            currency_code="USD",
        )
        sec = _make_security(security_id="sec-1", security_type="stock")
        acct = _make_account(account_id="acct-1")

        h_result = MagicMock()
        h_result.scalars.return_value.all.return_value = [h]
        a_result = MagicMock()
        a_result.scalars.return_value.all.return_value = [acct]
        s_result = MagicMock()
        s_result.scalars.return_value.all.return_value = [sec]
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        mock_session.execute.side_effect = [
            h_result,
            a_result,
            s_result,
            empty,
            empty,
        ]

        fx_service = AsyncMock()
        fx_service.convert.return_value = None  # conversion fails
        svc = AllocationService(mock_session, fx_service=fx_service)
        result = await svc.get_allocation(
            tenant_id="t1", target_currency="EUR"
        )

        # Falls back to original value when FX fails
        assert result.total_value == Decimal(1000)
        # Currency is still the requested target (design choice)
        assert result.currency_code == "EUR"
        fx_service.convert.assert_called_once()
