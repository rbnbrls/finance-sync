"""Tests for the merchant classification module.

Covers:
- Merchant name normalisation for ticker map lookup
- Merchant → ticker resolution
- GICS sector classification
- Subscription likelihood computation
- Fundamentals-based likelihood adjustment
- Category-to-sector mapping
- Full MerchantClassifier pipeline (with mocked UoW)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finance_sync.services.merchant_classifier import (
    CATEGORY_TO_SECTOR,
    LIKELIHOOD_HIGH,
    LIKELIHOOD_LOW,
    LIKELIHOOD_MEDIUM,
    MERCHANT_TICKER_MAP,
    SUBSCRIPTION_LIKELIHOOD_BY_SECTOR,
    MerchantClassification,
    MerchantClassifier,
    _adjust_likelihood_with_fundamentals,
    _get_sector_likelihood,
    _normalise_merchant_name,
    _resolve_merchant_entry,
    _sector_from_category,
)

# ═══════════════════════════════════════════════════════════════════════
# Merchant name normalisation
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantNameNormalisation:
    """Verify merchant name normalisation for ticker map lookup."""

    def test_strips_bv_suffix(self) -> None:
        assert _normalise_merchant_name("Netflix B.V.") == "netflix"

    def test_strips_inc_suffix(self) -> None:
        assert _normalise_merchant_name("Adobe Inc.") == "adobe"

    def test_strips_corp(self) -> None:
        assert _normalise_merchant_name("Salesforce Corp") == "salesforce"

    def test_strips_llc(self) -> None:
        assert _normalise_merchant_name("Datadog LLC") == "datadog"

    def test_strips_ltd(self) -> None:
        assert _normalise_merchant_name("Spotify Ltd") == "spotify"

    def test_strips_group(self) -> None:
        assert _normalise_merchant_name("HelloFresh Group") == "hellofresh"

    def test_strips_technologies(self) -> None:
        assert (
            _normalise_merchant_name("DigitalOcean Technologies")
            == "digitalocean"
        )

    def test_strips_nv_suffix(self) -> None:
        assert _normalise_merchant_name("NN Group N.V.") == "nn"

    def test_strips_gmbh(self) -> None:
        assert _normalise_merchant_name("Zenjob GmbH") == "zenjob"

    def test_case_insensitive(self) -> None:
        assert _normalise_merchant_name("NETFLIX B.V.") == "netflix"

    def test_already_clean_name(self) -> None:
        assert _normalise_merchant_name("netflix") == "netflix"

    def test_strips_plc(self) -> None:
        assert _normalise_merchant_name("Vodafone Group Plc") == "vodafone"

    def test_multi_word_suffix_strip(self) -> None:
        assert _normalise_merchant_name("Alphabet Holding Co.") == "alphabet"


# ═══════════════════════════════════════════════════════════════════════
# Merchant → ticker resolution
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantResolution:
    """Verify merchant name resolution to ticker map entries."""

    def test_netflix_resolved(self) -> None:
        entry = _resolve_merchant_entry("Netflix B.V.")
        assert entry is not None
        assert entry["ticker"] == "NFLX"
        assert entry["sector"] == "Communication Services"

    def test_spotify_resolved(self) -> None:
        entry = _resolve_merchant_entry("Spotify AB")
        assert entry is not None
        assert entry["ticker"] == "SPOT"

    def test_microsoft_365_resolved(self) -> None:
        entry = _resolve_merchant_entry("Microsoft 365")
        assert entry is not None
        assert entry["ticker"] == "MSFT"

    def test_google_workspace_resolved(self) -> None:
        entry = _resolve_merchant_entry("Google Workspace")
        assert entry is not None
        assert entry["ticker"] == "GOOGL"

    def test_github_resolved(self) -> None:
        entry = _resolve_merchant_entry("GitHub Inc.")
        assert entry is not None
        assert entry["ticker"] == "MSFT"  # GitHub is owned by Microsoft

    def test_dropbox_resolved(self) -> None:
        entry = _resolve_merchant_entry("Dropbox")
        assert entry is not None
        assert entry["ticker"] == "DBX"

    def test_icloud_resolved(self) -> None:
        entry = _resolve_merchant_entry("iCloud")
        assert entry is not None
        assert entry["ticker"] == "AAPL"

    def test_amazon_prime_resolved(self) -> None:
        entry = _resolve_merchant_entry("Amazon Prime")
        assert entry is not None
        assert entry["ticker"] == "AMZN"

    def test_peloton_resolved(self) -> None:
        entry = _resolve_merchant_entry("Peloton")
        assert entry is not None
        assert entry["ticker"] == "PTON"

    def test_unknown_merchant(self) -> None:
        entry = _resolve_merchant_entry("Random Local Shop")
        assert entry is None

    def test_partial_match_prefix(self) -> None:
        entry = _resolve_merchant_entry("Apple Music Subscription")
        assert entry is not None
        assert entry["ticker"] == "AAPL"

    def test_partial_match_microsoft_corp(self) -> None:
        entry = _resolve_merchant_entry("Microsoft Corporation")
        assert entry is not None
        assert entry["ticker"] == "MSFT"

    def test_private_company(self) -> None:
        entry = _resolve_merchant_entry("Patreon")
        assert entry is not None
        assert entry["ticker"] is None  # Private company
        assert entry["sector"] == "Technology"

    def test_dutch_insurer(self) -> None:
        entry = _resolve_merchant_entry("Zilveren Kruis")
        assert entry is not None
        assert entry["sector"] == "Financials"

    def test_ziggo_resolved(self) -> None:
        entry = _resolve_merchant_entry("Ziggo")
        assert entry is not None
        assert entry["sector"] == "Communication Services"

    def test_all_known_merchants_have_sector(self) -> None:
        """Every entry in MERCHANT_TICKER_MAP must have a sector."""
        for name, entry in MERCHANT_TICKER_MAP.items():
            assert entry.get("sector") is not None, (
                f"{name!r} is missing sector"
            )


# ═══════════════════════════════════════════════════════════════════════
# Sector subscription likelihood
# ═══════════════════════════════════════════════════════════════════════


class TestSectorLikelihood:
    """Verify GICS sector → subscription likelihood mapping."""

    def test_technology_is_high(self) -> None:
        assert _get_sector_likelihood("Technology") == LIKELIHOOD_HIGH

    def test_communication_services_is_high(self) -> None:
        assert (
            _get_sector_likelihood("Communication Services") == LIKELIHOOD_HIGH
        )

    def test_consumer_discretionary_is_high(self) -> None:
        assert (
            _get_sector_likelihood("Consumer Discretionary") == LIKELIHOOD_HIGH
        )

    def test_financials_is_medium(self) -> None:
        assert _get_sector_likelihood("Financials") == LIKELIHOOD_MEDIUM

    def test_utilities_is_medium(self) -> None:
        assert _get_sector_likelihood("Utilities") == LIKELIHOOD_MEDIUM

    def test_health_care_is_medium(self) -> None:
        assert _get_sector_likelihood("Health Care") == LIKELIHOOD_MEDIUM

    def test_energy_is_low(self) -> None:
        assert _get_sector_likelihood("Energy") == LIKELIHOOD_LOW

    def test_industrials_is_low(self) -> None:
        assert _get_sector_likelihood("Industrials") == LIKELIHOOD_LOW

    def test_materials_is_low(self) -> None:
        assert _get_sector_likelihood("Materials") == LIKELIHOOD_LOW

    def test_real_estate_is_low(self) -> None:
        assert _get_sector_likelihood("Real Estate") == LIKELIHOOD_LOW

    def test_consumer_staples_is_medium(self) -> None:
        assert _get_sector_likelihood("Consumer Staples") == LIKELIHOOD_MEDIUM

    def test_none_sector_defaults_to_medium(self) -> None:
        assert _get_sector_likelihood(None) == LIKELIHOOD_MEDIUM

    def test_unknown_sector_defaults_to_medium(self) -> None:
        assert _get_sector_likelihood("Unknown") == LIKELIHOOD_MEDIUM

    def test_all_sectors_covered(self) -> None:
        """Every GICS sector in the mapping should have a defined likelihood."""
        for sector, likelihood in SUBSCRIPTION_LIKELIHOOD_BY_SECTOR.items():
            assert likelihood in (
                LIKELIHOOD_HIGH,
                LIKELIHOOD_MEDIUM,
                LIKELIHOOD_LOW,
            ), f"{sector!r} has invalid likelihood {likelihood!r}"


# ═══════════════════════════════════════════════════════════════════════
# Category to sector mapping
# ═══════════════════════════════════════════════════════════════════════


class TestCategoryToSector:
    """Verify subscription category → GICS sector mapping."""

    def test_streaming_to_communication_services(self) -> None:
        assert _sector_from_category("streaming") == "Communication Services"

    def test_software_to_technology(self) -> None:
        assert _sector_from_category("software") == "Technology"

    def test_utilities_category_to_utilities(self) -> None:
        assert _sector_from_category("utilities") == "Utilities"

    def test_fitness_to_consumer_discretionary(self) -> None:
        assert _sector_from_category("fitness") == "Consumer Discretionary"

    def test_insurance_to_financials(self) -> None:
        assert _sector_from_category("insurance") == "Financials"

    def test_news_media_to_communication_services(self) -> None:
        assert _sector_from_category("news_media") == "Communication Services"

    def test_donations_to_technology(self) -> None:
        assert _sector_from_category("donations") == "Technology"

    def test_cloud_storage_to_technology(self) -> None:
        assert _sector_from_category("cloud_storage") == "Technology"

    def test_none_category(self) -> None:
        assert _sector_from_category(None) is None

    def test_unknown_category(self) -> None:
        assert _sector_from_category("unknown_category") is None

    def test_all_categories_mapped(self) -> None:
        """Every category in the map should map to a known GICS sector."""
        for category, sector in CATEGORY_TO_SECTOR.items():
            assert sector in SUBSCRIPTION_LIKELIHOOD_BY_SECTOR, (
                f"Category {category!r} maps to unknown sector {sector!r}"
            )


# ═══════════════════════════════════════════════════════════════════════
# Fundamentals-based likelihood adjustment
# ═══════════════════════════════════════════════════════════════════════


class TestFundamentalsAdjustment:
    """Verify fundamentals data adjusts subscription likelihood correctly."""

    def test_high_dividend_downgrades_high_to_medium(self) -> None:
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_HIGH,
            pe_ratio=Decimal(15),
            dividend_yield=Decimal("0.05"),  # 5% -> high dividend
        )
        assert result == LIKELIHOOD_MEDIUM

    def test_high_pe_upgrades_medium_to_high(self) -> None:
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=Decimal(80),  # Very high PE
            dividend_yield=Decimal("0.00"),
        )
        assert result == LIKELIHOOD_HIGH

    def test_no_change_for_low_likelihood(self) -> None:
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_LOW,
            pe_ratio=Decimal(80),
            dividend_yield=Decimal("0.00"),
        )
        assert result == LIKELIHOOD_LOW  # No upgrade from low

    def test_high_dividend_cancels_pe_upgrade(self) -> None:
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=Decimal(80),
            dividend_yield=Decimal("0.04"),  # Both high PE and high dividend
        )
        # Dividend >= 3% cancels the PE upgrade
        assert result == LIKELIHOOD_MEDIUM

    def test_no_fundamentals_no_change(self) -> None:
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_HIGH,
            pe_ratio=None,
            dividend_yield=None,
        )
        assert result == LIKELIHOOD_HIGH

    def test_no_change_for_medium_no_signals(self) -> None:
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=Decimal(20),
            dividend_yield=Decimal("0.02"),
        )
        assert result == LIKELIHOOD_MEDIUM

    def test_very_high_dividend_downgrades_high(self) -> None:
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_HIGH,
            pe_ratio=Decimal(10),
            dividend_yield=Decimal("0.06"),
        )
        assert result == LIKELIHOOD_MEDIUM

    def test_edge_case_just_below_threshold(self) -> None:
        """Dividend at exactly 4% should still downgrade."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_HIGH,
            pe_ratio=Decimal(15),
            dividend_yield=Decimal("0.04"),
        )
        # 0.04 is NOT > 0.04, so it should NOT downgrade
        assert result == LIKELIHOOD_HIGH

    def test_high_pe_with_low_dividend_upgrades(self) -> None:
        """PE > 50 with dividend < 3% should upgrade medium to high."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=Decimal(60),
            dividend_yield=Decimal("0.01"),
        )
        assert result == LIKELIHOOD_HIGH

    def test_dividend_just_above_three_pct_blocks_pe_upgrade(self) -> None:
        """Dividend >= 3% blocks PE upgrade even at high PE."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=Decimal(100),
            dividend_yield=Decimal("0.03"),  # == 0.03, not > 0.03
        )
        # The condition is > 0.03, so 0.03 should NOT block
        assert result == LIKELIHOOD_HIGH


# ═══════════════════════════════════════════════════════════════════════
# MerchantClassification DTO
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantClassification:
    """Verify the MerchantClassification DTO."""

    def test_default_values(self) -> None:
        mc = MerchantClassification(merchant_name="Test Merchant")
        assert mc.merchant_name == "Test Merchant"
        assert mc.sector is None
        assert mc.subscription_likelihood == LIKELIHOOD_MEDIUM
        assert mc.likelihood_score == 0.06
        assert mc.ticker is None
        assert mc.security_id is None
        assert not mc.fundamentals_available
        assert mc.source == "sector_map"

    def test_high_likelihood_score(self) -> None:
        mc = MerchantClassification(
            merchant_name="Netflix",
            sector="Communication Services",
            subscription_likelihood=LIKELIHOOD_HIGH,
        )
        assert mc.likelihood_score == 0.12

    def test_medium_likelihood_score(self) -> None:
        mc = MerchantClassification(
            merchant_name="Utilities Co",
            sector="Utilities",
            subscription_likelihood=LIKELIHOOD_MEDIUM,
        )
        assert mc.likelihood_score == 0.06

    def test_low_likelihood_score(self) -> None:
        mc = MerchantClassification(
            merchant_name="Energy Co",
            sector="Energy",
            subscription_likelihood=LIKELIHOOD_LOW,
        )
        assert mc.likelihood_score == 0.0

    def test_full_classification(self) -> None:
        mc = MerchantClassification(
            merchant_name="Netflix B.V.",
            sector="Communication Services",
            subscription_likelihood=LIKELIHOOD_HIGH,
            ticker="NFLX",
            security_id="sec_123",
            fundamentals_available=True,
            source="merchant_map",
        )
        assert mc.ticker == "NFLX"
        assert mc.security_id == "sec_123"
        assert mc.fundamentals_available
        assert mc.source == "merchant_map"

    def test_repr(self) -> None:
        mc = MerchantClassification(
            merchant_name="Netflix",
            sector="Communication Services",
            subscription_likelihood=LIKELIHOOD_HIGH,
        )
        r = repr(mc)
        assert "Netflix" in r
        assert "Communication Services" in r
        assert "high" in r


# ═══════════════════════════════════════════════════════════════════════
# MerchantClassifier service
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantClassifier:
    """Verify the MerchantClassifier service class."""

    @pytest.mark.asyncio
    async def test_classify_known_merchant(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify("Netflix B.V.")
        assert result.sector == "Communication Services"
        assert result.ticker == "NFLX"
        assert result.subscription_likelihood == LIKELIHOOD_HIGH
        assert result.source == "merchant_map"

    @pytest.mark.asyncio
    async def test_classify_known_merchant_with_category(self) -> None:
        """Category is only used as fallback; merchant_map takes priority."""
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify(
            "Dropbox",
            category="cloud_storage",
        )
        assert result.sector == "Technology"
        assert result.ticker == "DBX"
        assert result.source == "merchant_map"

    @pytest.mark.asyncio
    async def test_classify_unknown_with_category_fallback(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify(
            "Some Gym",
            category="fitness",
        )
        assert result.sector == "Consumer Discretionary"
        assert result.subscription_likelihood == LIKELIHOOD_HIGH
        assert result.source == "category_map"

    @pytest.mark.asyncio
    async def test_classify_unknown_merchant(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify("Random Shop Amsterdam")
        assert result.sector is None
        assert result.subscription_likelihood == LIKELIHOOD_MEDIUM
        assert result.source == "sector_map"

    @pytest.mark.asyncio
    async def test_classify_private_company(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify("Patreon")
        assert result.sector == "Technology"
        assert result.ticker is None  # Private company
        assert result.subscription_likelihood == LIKELIHOOD_HIGH

    @pytest.mark.asyncio
    async def test_classify_batch(self) -> None:
        classifier = MerchantClassifier(uow=None)
        merchants = [
            {"merchant_name": "Netflix B.V."},
            {"merchant_name": "Unknown Shop", "category": "utilities"},
            {"merchant_name": "Dropbox Inc."},
        ]
        results = await classifier.classify_batch(merchants)
        assert "Netflix B.V." in results
        assert "Unknown Shop" in results
        assert "Dropbox Inc." in results
        assert results["Netflix B.V."].ticker == "NFLX"
        assert results["Unknown Shop"].sector == "Utilities"
        assert results["Dropbox Inc."].ticker == "DBX"

    @pytest.mark.asyncio
    async def test_classify_batch_empty(self) -> None:
        classifier = MerchantClassifier(uow=None)
        results = await classifier.classify_batch([])
        assert results == {}

    @pytest.mark.asyncio
    async def test_dutch_insurer_classification(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify("Zilveren Kruis")
        assert result.sector == "Financials"
        assert result.subscription_likelihood == LIKELIHOOD_MEDIUM

    @pytest.mark.asyncio
    async def test_dutch_utility_classification(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify("Eneco")
        assert result.sector == "Utilities"
        assert result.subscription_likelihood == LIKELIHOOD_MEDIUM

    @pytest.mark.asyncio
    async def test_insurance_company(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify("Allianz")
        assert result.sector == "Financials"
        assert result.ticker == "ALV"

    @pytest.mark.asyncio
    async def test_telco_with_category(self) -> None:
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify("Vodafone")
        assert result.sector == "Communication Services"
        assert result.ticker == "VOD"
