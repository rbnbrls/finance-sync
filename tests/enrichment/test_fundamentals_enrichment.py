"""Tests for fundamentals and ETF metadata enrichment schemas and services.

Covers:
- FundamentalObservationData and related DTOs
- ETF Composition DTOs (ETFHolding, SectorExposure, etc.)
- Gateway fundamentals and ETF endpoints
- MetadataEnricher service (classify_sector, enrich_security)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.gateway import EnrichmentGateway, _safe_decimal
from finance_sync.enrichment.metadata_enricher import (
    GICS_SECTORS,
    MetadataEnricher,
    _to_decimal,
)
from finance_sync.enrichment.models import (
    ETFComposition,
    ETFHolding,
    FundamentalObservationData,
    FundamentalRatioSummary,
    RegionExposure,
    SectorExposure,
    SecurityMetadataObservationData,
)

# =====================================================================
# Schema / DTO tests
# =====================================================================


class TestFundamentalObservationData:
    """Tests for the FundamentalObservationData Pydantic model."""

    def test_minimal_construction(self) -> None:
        """Can construct with just required fields."""
        now = datetime.now(UTC)
        obs = FundamentalObservationData(
            security_id="sec_1",
            timestamp=now,
            source="openbb",
        )
        assert obs.security_id == "sec_1"
        assert obs.timestamp == now
        assert obs.pe_ratio is None
        assert obs.source == "openbb"

    def test_full_construction(self) -> None:
        """Can construct with all fundamental fields."""
        now = datetime.now(UTC)
        obs = FundamentalObservationData(
            security_id="sec_1",
            timestamp=now,
            pe_ratio=Decimal("22.5"),
            forward_pe=Decimal("20.1"),
            peg_ratio=Decimal("1.5"),
            eps=Decimal("5.20"),
            eps_forward=Decimal("5.80"),
            book_value_per_share=Decimal("15.00"),
            dividend_yield=Decimal("0.025"),
            dividend_rate=Decimal("1.20"),
            market_cap=Decimal(3000000000000),
            enterprise_value=Decimal(3100000000000),
            shares_outstanding=Decimal(15000000000),
            beta=Decimal("1.2"),
            high_52w=Decimal("198.00"),
            low_52w=Decimal("145.00"),
            source="openbb",
            provider_metadata={"sector": "Technology"},
        )
        assert obs.pe_ratio == Decimal("22.5")
        assert obs.market_cap == Decimal(3000000000000)
        assert obs.beta == Decimal("1.2")
        assert obs.dividend_yield == Decimal("0.025")
        assert obs.provider_metadata == {"sector": "Technology"}

    def test_serialization(self) -> None:
        """DTO serializes to JSON-compatible dict."""
        now = datetime.now(UTC)
        obs = FundamentalObservationData(
            security_id="sec_1",
            timestamp=now,
            pe_ratio=Decimal("15.5"),
        )
        data = obs.model_dump(mode="json")
        assert data["security_id"] == "sec_1"
        assert data["pe_ratio"] == "15.5"
        assert data["source"] == "openbb"


class TestFundamentalRatioSummary:
    """Tests for the condensed ratio summary."""

    def test_construction(self) -> None:
        summary = FundamentalRatioSummary(
            pe_ratio=Decimal("22.5"),
            forward_pe=Decimal("20.1"),
            dividend_yield=Decimal("0.025"),
            eps=Decimal("5.20"),
            market_cap=Decimal(3000000000000),
            beta=Decimal("1.2"),
        )
        assert summary.pe_ratio == Decimal("22.5")
        assert summary.market_cap == Decimal(3000000000000)

    def test_empty(self) -> None:
        summary = FundamentalRatioSummary()
        assert summary.pe_ratio is None
        assert summary.dividend_yield is None


class TestSecurityMetadataObservationData:
    """Tests for the SecurityMetadataObservationData DTO."""

    def test_etf_composition(self) -> None:
        now = datetime.now(UTC)
        obs = SecurityMetadataObservationData(
            security_id="sec_1",
            metadata_type="etf_composition",
            timestamp=now,
            metadata_json={
                "etf_name": "Vanguard S&P 500 ETF",
                "total_holdings": 500,
                "expense_ratio": "0.0003",
            },
            label="VOO",
            source="openbb",
        )
        assert obs.metadata_type == "etf_composition"
        assert obs.metadata_json["etf_name"] == "Vanguard S&P 500 ETF"

    def test_sector_exposure(self) -> None:
        now = datetime.now(UTC)
        obs = SecurityMetadataObservationData(
            security_id="sec_1",
            metadata_type="sector_exposure",
            timestamp=now,
            metadata_json={
                "primary_sector": "Technology",
                "sector_exposures": [
                    {"sector": "Technology", "weight": "1.0"}
                ],
            },
            label="Technology",
            source="openbb",
        )
        assert obs.metadata_type == "sector_exposure"


class TestETFModels:
    """Tests for ETF composition DTOs."""

    def test_etf_holding(self) -> None:
        h = ETFHolding(
            ticker="AAPL",
            name="Apple Inc.",
            weight=Decimal("0.07"),
            sector="Technology",
            market_value=Decimal(150000000000),
            shares=Decimal(1000000),
        )
        assert h.ticker == "AAPL"
        assert h.weight == Decimal("0.07")

    def test_sector_exposure_dto(self) -> None:
        s = SectorExposure(sector="Technology", weight=Decimal("0.25"))
        assert s.sector == "Technology"
        assert s.weight == Decimal("0.25")

    def test_region_exposure(self) -> None:
        r = RegionExposure(region="North America", weight=Decimal("0.65"))
        assert r.region == "North America"
        assert r.weight == Decimal("0.65")

    def test_etf_composition(self) -> None:
        comp = ETFComposition(
            etf_name="VOO",
            total_holdings=500,
            holdings=[
                ETFHolding(ticker="AAPL", weight=Decimal("0.07")),
                ETFHolding(ticker="MSFT", weight=Decimal("0.06")),
            ],
            sector_exposures=[
                SectorExposure(sector="Technology", weight=Decimal("0.30"))
            ],
            region_exposures=[
                RegionExposure(region="US", weight=Decimal("0.98"))
            ],
            expense_ratio=Decimal("0.0003"),
            dividend_yield=Decimal("0.015"),
        )
        assert comp.etf_name == "VOO"
        assert len(comp.holdings) == 2
        assert len(comp.sector_exposures) == 1
        assert comp.expense_ratio == Decimal("0.0003")


# =====================================================================
# Gateway method tests
# =====================================================================


class TestGatewayFundamentals:
    """Tests for EnrichmentGateway fundamentals endpoint integration."""

    @pytest.fixture
    def settings(self):
        s = MagicMock()
        s.openbb_api_key = MagicMock()
        s.openbb_api_key.get_secret_value.return_value = "sk-test-key"
        s.openbb_base_url = "https://openbb.co/api"
        s.openbb_api_version = "v1"
        s.openbb_request_timeout = 30
        return s

    @pytest.fixture
    def mock_uow(self):
        return MagicMock()

    @pytest.fixture
    def mock_price_store(self):
        return AsyncMock()

    @pytest.fixture
    def gateway(self, settings, mock_uow, mock_price_store):
        g = EnrichmentGateway(
            settings=settings,
            uow=mock_uow,
            price_store=mock_price_store,
        )
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock()
        g._http_client = mock_client
        return g

    def _make_mock_response(self, status_code=200, json_data=None):
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.json.return_value = json_data or {}
        if status_code >= 400:
            import httpx

            mock_response.raise_for_status.side_effect = (
                httpx.HTTPStatusError(
                    "Error", request=MagicMock(), response=mock_response
                )
            )
        else:
            mock_response.raise_for_status = MagicMock()
        return mock_response

    def test_not_degraded(self, gateway) -> None:
        assert not gateway.is_degraded

    async def test_get_fundamentals_success(self, gateway) -> None:
        """get_fundamentals returns parsed data on success."""
        mock_response = self._make_mock_response(
            json_data={
                "peRatio": 22.5,
                "forwardPE": 20.1,
                "eps": 5.20,
                "marketCap": 3000000000000,
                "dividendYield": 0.025,
                "beta": 1.2,
                "fiftyTwoWeekHigh": 198.00,
                "fiftyTwoWeekLow": 145.00,
                "sector": "Technology",
                "sharesOutstanding": 15000000000,
            }
        )
        gateway._http_client.get.return_value = mock_response

        result = await gateway.get_fundamentals("AAPL", "ticker")
        assert result is not None
        assert result.pe_ratio == Decimal("22.5")
        assert result.forward_pe == Decimal("20.1")
        assert result.eps == Decimal("5.20")
        assert result.market_cap == Decimal(3000000000000)
        assert result.dividend_yield == Decimal("0.025")
        assert result.beta == Decimal("1.2")
        assert result.high_52w == Decimal("198.00")
        assert result.low_52w == Decimal("145.00")
        assert result.provider_metadata is not None
        assert result.provider_metadata["sector"] == "Technology"

    async def test_get_fundamentals_degraded(self, settings, mock_uow, mock_price_store) -> None:
        """get_fundamentals returns None in degraded mode."""
        settings.openbb_api_key = None
        g = EnrichmentGateway(
            settings=settings,
            uow=mock_uow,
            price_store=mock_price_store,
        )
        result = await g.get_fundamentals("AAPL")
        assert result is None

    async def test_get_fundamentals_not_found(self, gateway) -> None:
        """get_fundamentals returns None on 404."""
        mock_response = self._make_mock_response(
            status_code=404, json_data={}
        )
        gateway._http_client.get.return_value = mock_response

        result = await gateway.get_fundamentals("UNKNOWN")
        assert result is None

    async def test_get_fundamentals_timeout(self, gateway) -> None:
        """get_fundamentals returns None on timeout."""
        import httpx

        gateway._http_client.get.side_effect = httpx.TimeoutException(
            "Timeout"
        )

        result = await gateway.get_fundamentals("AAPL")
        assert result is None


class TestGatewayETFComposition:
    """Tests for EnrichmentGateway ETF composition endpoint."""

    @pytest.fixture
    def settings(self):
        s = MagicMock()
        s.openbb_api_key = MagicMock()
        s.openbb_api_key.get_secret_value.return_value = "sk-test-key"
        s.openbb_base_url = "https://openbb.co/api"
        s.openbb_api_version = "v1"
        s.openbb_request_timeout = 30
        return s

    @pytest.fixture
    def mock_uow(self):
        return MagicMock()

    @pytest.fixture
    def mock_price_store(self):
        return AsyncMock()

    @pytest.fixture
    def gateway(self, settings, mock_uow, mock_price_store):
        g = EnrichmentGateway(
            settings=settings,
            uow=mock_uow,
            price_store=mock_price_store,
        )
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock()
        g._http_client = mock_client
        return g

    def _make_mock_response(self, status_code=200, json_data=None):
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.json.return_value = json_data or {}
        if status_code >= 400:
            import httpx

            mock_response.raise_for_status.side_effect = (
                httpx.HTTPStatusError(
                    "Error", request=MagicMock(), response=mock_response
                )
            )
        else:
            mock_response.raise_for_status = MagicMock()
        return mock_response

    async def test_get_etf_composition_success(self, gateway) -> None:
        """get_etf_composition returns parsed ETF data."""
        mock_response = self._make_mock_response(
            json_data={
                "name": "Vanguard S&P 500 ETF",
                "totalHoldings": 507,
                "holdings": [
                    {
                        "ticker": "AAPL",
                        "name": "Apple Inc.",
                        "weight": 0.07,
                        "sector": "Technology",
                    },
                    {
                        "ticker": "MSFT",
                        "name": "Microsoft Corp.",
                        "weight": 0.06,
                        "sector": "Technology",
                    },
                ],
                "sectorExposures": [
                    {"sector": "Technology", "weight": 0.30},
                    {"sector": "Health Care", "weight": 0.13},
                ],
                "regionExposures": [
                    {"region": "United States", "weight": 0.98},
                    {"region": "Europe", "weight": 0.01},
                ],
                "expenseRatio": 0.0003,
                "dividendYield": 0.015,
            }
        )
        gateway._http_client.get.return_value = mock_response

        result = await gateway.get_etf_composition("VOO")
        assert result is not None
        assert result.etf_name == "Vanguard S&P 500 ETF"
        assert result.total_holdings == 507
        assert len(result.holdings) == 2
        assert result.holdings[0].ticker == "AAPL"
        assert result.holdings[0].weight == Decimal("0.07")
        assert len(result.sector_exposures) == 2
        assert result.sector_exposures[0].sector == "Technology"
        assert result.sector_exposures[0].weight == Decimal("0.30")
        assert len(result.region_exposures) == 2
        assert result.region_exposures[0].region == "United States"
        assert result.expense_ratio == Decimal("0.0003")
        assert result.dividend_yield == Decimal("0.015")

    async def test_get_etf_composition_no_holdings(self, gateway) -> None:
        """get_etf_composition handles empty holdings gracefully."""
        mock_response = self._make_mock_response(
            json_data={
                "name": "Empty ETF",
                "holdings": [],
                "sectorExposures": [],
                "regionExposures": [],
            }
        )
        gateway._http_client.get.return_value = mock_response

        result = await gateway.get_etf_composition("EMPTY")
        assert result is not None
        assert result.holdings == []
        assert result.total_holdings == 0

    async def test_get_etf_composition_snake_case_fields(self, gateway) -> None:
        """Handles snake_case field names from alternative providers."""
        mock_response = self._make_mock_response(
            json_data={
                "etf_name": "iShares Core S&P 500",
                "total_holdings": 505,
                "holdings": [
                    {"symbol": "AAPL", "weight": 0.07},
                ],
                "sector_exposures": [
                    {"name": "Information Technology", "exposure": 0.30},
                ],
                "region_exposures": [
                    {"name": "North America", "exposure": 0.99},
                ],
                "expense_ratio": 0.0003,
            }
        )
        gateway._http_client.get.return_value = mock_response

        result = await gateway.get_etf_composition("IVV")
        assert result is not None
        assert result.etf_name == "iShares Core S&P 500"
        assert result.holdings[0].ticker == "AAPL"
        assert result.holdings[0].weight == Decimal("0.07")
        assert result.sector_exposures[0].sector == "Information Technology"

    async def test_get_etf_composition_degraded(self, gateway) -> None:
        """Returns None in degraded mode."""
        gateway._degraded = True
        result = await gateway.get_etf_composition("VOO")
        assert result is None


# =====================================================================
# MetadataEnricher tests
# =====================================================================


class TestMetadataEnricher:
    """Tests for MetadataEnricher service."""

    @pytest.fixture
    def mock_gateway(self):
        return AsyncMock()

    @pytest.fixture
    def mock_uow(self):
        uow = MagicMock()
        uow.fundamental_observations = AsyncMock()
        uow.security_metadata_observations = AsyncMock()
        uow.fundamental_observations.list = AsyncMock(return_value=[])
        uow.fundamental_observations.add = AsyncMock()
        uow.security_metadata_observations.list = AsyncMock(return_value=[])
        uow.security_metadata_observations.add = AsyncMock()
        return uow

    @pytest.fixture
    def enricher(self, mock_uow, mock_gateway):
        return MetadataEnricher(uow=mock_uow, gateway=mock_gateway)

    # ── Sector classification ───────────────────────────────────────

    def test_classify_sector_exact(self, enricher) -> None:
        assert enricher.classify_sector("Technology") == "Technology"
        assert enricher.classify_sector("Health Care") == "Health Care"
        assert enricher.classify_sector("Real Estate") == "Real Estate"

    def test_classify_sector_case_insensitive(self, enricher) -> None:
        assert enricher.classify_sector("technology") == "Technology"
        assert enricher.classify_sector("HEALTH CARE") == "Health Care"

    def test_classify_sector_keyword_match(self, enricher) -> None:
        assert enricher.classify_sector("Semiconductors") == "Technology"
        assert enricher.classify_sector("Pharmaceuticals") == "Health Care"
        assert enricher.classify_sector("Banking") == "Financials"
        assert (
            enricher.classify_sector("Oil & Gas Exploration")
            == "Energy"
        )
        assert (
            enricher.classify_sector("Software & Services")
            == "Technology"
        )

    def test_classify_sector_none(self, enricher) -> None:
        assert enricher.classify_sector(None) is None
        assert enricher.classify_sector("") is None

    def test_classify_sector_unknown(self, enricher) -> None:
        assert enricher.classify_sector("Unknown Sector") is None

    def test_classify_sector_exposures(self, enricher) -> None:
        raw = [
            {"sector": "Technology", "weight": 0.30},
            {"sector": "Health Care", "exposure": 0.13},
            {"name": "Financials", "percentage": 0.10},
        ]
        exposures = enricher.classify_sector_exposures(raw)
        assert len(exposures) == 3
        assert exposures[0].sector == "Technology"
        assert exposures[0].weight == Decimal("0.30")
        assert exposures[1].sector == "Health Care"
        assert exposures[2].sector == "Financials"

    def test_classify_sector_exposures_empty(self, enricher) -> None:
        assert enricher.classify_sector_exposures([]) == []

    def test_classify_sector_exposures_skips_missing(self, enricher) -> None:
        raw = [
            {"sector": "Technology", "weight": 0.30},
            {"no_sector": "Missing", "weight": 0.10},
            {},
        ]
        exposures = enricher.classify_sector_exposures(raw)
        assert len(exposures) == 1
        assert exposures[0].sector == "Technology"

    # ── enrich_security ─────────────────────────────────────────────

    async def test_enrich_security_stock(
        self, enricher, mock_gateway
    ) -> None:
        """enrich_security fetches fundamentals for a stock."""
        mock_gateway.is_degraded = False
        mock_gateway.get_fundamentals.return_value = (
            FundamentalObservationData(
                security_id="",
                timestamp=datetime.now(UTC),
                pe_ratio=Decimal("22.5"),
                source="openbb",
                provider_metadata={"sector": "Technology"},
            )
        )
        mock_gateway.resolve_security.return_value = MagicMock(
            provider_metadata={"sector": "Technology"}
        )

        result = await enricher.enrich_security(
            security_id="sec_1",
            identifier="AAPL",
            identifier_type="ticker",
            security_type="stock",
        )
        assert result["security_id"] == "sec_1"
        assert result["fundamentals"] is True
        # ETF composition should be False for non-ETF
        assert result["etf_composition"] is False

    async def test_enrich_security_etf(
        self, enricher, mock_gateway
    ) -> None:
        """enrich_security fetches fundamentals + ETF composition for ETFs."""
        mock_gateway.is_degraded = False
        mock_gateway.get_fundamentals.return_value = (
            FundamentalObservationData(
                security_id="",
                timestamp=datetime.now(UTC),
                pe_ratio=Decimal("20.0"),
                source="openbb",
            )
        )
        mock_gateway.get_etf_composition.return_value = ETFComposition(
            etf_name="VOO",
            total_holdings=500,
            holdings=[],
            sector_exposures=[],
            region_exposures=[],
        )
        mock_gateway.resolve_security.return_value = MagicMock(
            provider_metadata={"sector": "Financials"}
        )

        result = await enricher.enrich_security(
            security_id="sec_2",
            identifier="VOO",
            security_type="etf",
        )
        assert result["fundamentals"] is True
        assert result["etf_composition"] is True

    async def test_enrich_security_degraded(
        self, enricher, mock_gateway
    ) -> None:
        """enrich_security returns all False in degraded mode."""
        mock_gateway.is_degraded = True
        mock_gateway.get_fundamentals.return_value = None

        result = await enricher.enrich_security(
            security_id="sec_1",
            identifier="AAPL",
        )
        assert result["fundamentals"] is False
        assert result["etf_composition"] is False

    # ── compute_ratio_summary ───────────────────────────────────────

    async def test_compute_ratio_summary_no_data(
        self, enricher, mock_uow
    ) -> None:
        """compute_ratio_summary returns None when no data exists."""
        mock_uow.fundamental_observations.list.return_value = []
        result = await enricher.compute_ratio_summary("sec_1")
        assert result is None

    async def test_get_recent_fundamentals(
        self, enricher, mock_uow
    ) -> None:
        """get_recent_fundamentals returns recent observations."""
        from finance_sync.models.fundamental_observation import (
            FundamentalObservation,
        )

        mock_obs = MagicMock(spec=FundamentalObservation)
        mock_obs.security_id = "sec_1"
        mock_obs.timestamp = datetime.now(UTC)
        mock_obs.pe_ratio = Decimal("22.5")
        mock_obs.forward_pe = None
        mock_obs.peg_ratio = None
        mock_obs.eps = Decimal("5.20")
        mock_obs.eps_forward = None
        mock_obs.book_value_per_share = None
        mock_obs.dividend_yield = Decimal("0.025")
        mock_obs.dividend_rate = None
        mock_obs.market_cap = Decimal(3000000000000)
        mock_obs.enterprise_value = None
        mock_obs.shares_outstanding = None
        mock_obs.beta = Decimal("1.2")
        mock_obs.high_52w = None
        mock_obs.low_52w = None
        mock_obs.source = "openbb"
        mock_obs.provider_metadata = None

        mock_uow.fundamental_observations.list.return_value = [mock_obs]

        results = await enricher.get_recent_fundamentals("sec_1", limit=1)
        assert len(results) == 1
        assert results[0].pe_ratio == Decimal("22.5")


# =====================================================================
# Helper function tests
# =====================================================================


class TestHelpers:
    """Tests for module-level utility functions."""

    def test_to_decimal_none(self) -> None:
        assert _to_decimal(None) is None

    def test_to_decimal_value(self) -> None:
        assert _to_decimal(Decimal("10.5")) == Decimal("10.5")

    def test_safe_decimal_none(self) -> None:
        assert _safe_decimal(None) is None

    def test_safe_decimal_value(self) -> None:
        assert _safe_decimal(22.5) == Decimal("22.5")

    def test_gics_sectors_mapping(self) -> None:
        """GICS_SECTORS has well-known sector entries."""
        assert "technology" in GICS_SECTORS
        assert "healthcare" in GICS_SECTORS
        assert "financials" in GICS_SECTORS
        assert "energy" in GICS_SECTORS
        assert "utilities" in GICS_SECTORS
        assert "real estate" in GICS_SECTORS
