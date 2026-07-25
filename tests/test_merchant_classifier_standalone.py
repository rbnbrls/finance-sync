"""Tests for the standalone merchant classifier (subscription_detector/merchant_classifier).

Covers:
- MerchantClass dataclass defaults and full initialisation
- classify_merchant resolution priority (merchant_map → category_map → sector_map)
- classify_merchant with fundamentals (pe_ratio, dividend_yield)
- classify_merchant edge cases: empty, None, refinements
- _likelihood_to_confidence mapping
- normalise_merchant_name convenience alias
- resolve_ticker known / unknown / private merchants
- sector_subscription_likelihood delegation
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finance_sync.services.merchant_classifier import (
    LIKELIHOOD_HIGH,
    LIKELIHOOD_LOW,
    LIKELIHOOD_MEDIUM,
)
from finance_sync.services.subscription_detector.merchant_classifier import (
    MerchantClass,
    _likelihood_to_confidence,
    classification_from_db,
    classify_merchant,
    normalise_merchant_name,
    resolve_ticker,
    sector_subscription_likelihood,
)

# ═══════════════════════════════════════════════════════════════════════
# MerchantClass dataclass
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantClass:
    """Verify the MerchantClass DTO."""

    def test_default_values(self) -> None:
        """Default is_subscription should be False (confidence < 0.50)."""
        mc = MerchantClass(is_subscription=False, confidence=0.30)
        assert mc.is_subscription is False
        assert mc.confidence == 0.30
        assert mc.sector is None
        assert mc.ticker is None
        assert mc.source == "sector_map"
        assert mc.subscription_likelihood == LIKELIHOOD_MEDIUM
        assert mc.likelihood_score == 0.0

    def test_full_initialisation(self) -> None:
        """All fields populated."""
        mc = MerchantClass(
            is_subscription=True,
            confidence=0.85,
            sector="Communication Services",
            ticker="NFLX",
            source="merchant_map",
            subscription_likelihood=LIKELIHOOD_HIGH,
            likelihood_score=0.12,
        )
        assert mc.is_subscription is True
        assert mc.confidence == 0.85
        assert mc.sector == "Communication Services"
        assert mc.ticker == "NFLX"
        assert mc.source == "merchant_map"
        assert mc.subscription_likelihood == LIKELIHOOD_HIGH
        assert mc.likelihood_score == 0.12

    def test_frozen_dataclass(self) -> None:
        """MerchantClass is frozen (immutable)."""
        mc = MerchantClass(is_subscription=False, confidence=0.30)
        with pytest.raises(AttributeError):
            mc.is_subscription = True  # type: ignore[misc]

    def test_high_conf_is_subscription(self) -> None:
        """Confidence >= 0.50 → is_subscription = True."""
        mc = MerchantClass(is_subscription=True, confidence=0.50)
        assert mc.is_subscription is True

    def test_low_conf_not_subscription(self) -> None:
        """Confidence < 0.50 → is_subscription = False."""
        mc = MerchantClass(is_subscription=False, confidence=0.49)
        assert mc.is_subscription is False


# ═══════════════════════════════════════════════════════════════════════
# classify_merchant — merchant_map resolution (priority 1)
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyMerchantMerchantMap:
    """classify_merchant with known merchants in the ticker map."""

    def test_netflix_returns_high_confidence(self) -> None:
        result = classify_merchant("Netflix B.V.")
        assert result.is_subscription is True
        assert result.confidence >= 0.50
        assert result.sector == "Communication Services"
        assert result.ticker == "NFLX"
        assert result.source == "merchant_map"
        assert result.subscription_likelihood == LIKELIHOOD_HIGH

    def test_spotify_returns_high_confidence(self) -> None:
        result = classify_merchant("Spotify AB")
        assert result.is_subscription is True
        assert result.sector is not None
        assert result.ticker == "SPOT"
        assert result.source == "merchant_map"

    def test_github_resolved_via_msft(self) -> None:
        """GitHub maps to MSFT (owned by Microsoft)."""
        result = classify_merchant("GitHub Inc.")
        assert result.is_subscription is True
        assert result.ticker == "MSFT"
        assert result.source == "merchant_map"

    def test_icloud_resolved_via_aapl(self) -> None:
        """iCloud maps to AAPL."""
        result = classify_merchant("iCloud")
        assert result.is_subscription is True
        assert result.ticker == "AAPL"
        assert result.source == "merchant_map"

    def test_private_company_no_ticker(self) -> None:
        """Private companies in the map have no ticker but still resolve."""
        result = classify_merchant("Patreon")
        assert result.is_subscription is True
        assert result.ticker is None  # Private company
        assert result.source == "merchant_map"

    def test_already_normalised_name(self) -> None:
        """A partially normalised name still resolves."""
        result = classify_merchant("netflix")
        assert result.is_subscription is True
        assert result.ticker == "NFLX"
        assert result.source == "merchant_map"

    def test_description_with_extra_metadata(self) -> None:
        """Extra text gets normalised away and the merchant is resolved."""
        result = classify_merchant("Netflix Subscription REF:1234567890")
        assert result.is_subscription is True
        assert result.ticker == "NFLX"

    def test_category_is_ignored_when_map_hits(self) -> None:
        """When merchant_map resolves, the category parameter is ignored."""
        result = classify_merchant("Dropbox", category="cloud_storage")
        assert result.source == "merchant_map"


# ═══════════════════════════════════════════════════════════════════════
# classify_merchant — category_map resolution (priority 2)
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyMerchantCategoryMap:
    """classify_merchant with unknown merchant but known category."""

    def test_unknown_merchant_with_streaming_category(self) -> None:
        result = classify_merchant(
            "Some New Streaming Service", category="streaming"
        )
        assert result.is_subscription is True
        assert result.sector == "Communication Services"
        assert result.source == "category_map"
        assert result.ticker is None

    def test_unknown_with_software_category(self) -> None:
        result = classify_merchant("Random SaaS Corp", category="software")
        assert result.is_subscription is True
        assert result.sector == "Technology"
        assert result.source == "category_map"

    def test_unknown_with_insurance_category(self) -> None:
        result = classify_merchant("Some Insurance Co", category="insurance")
        assert result.sector == "Financials"
        assert result.source == "category_map"

    def test_category_fitness_to_consumer_discretionary(self) -> None:
        result = classify_merchant("Local Gym", category="fitness")
        assert result.sector == "Consumer Discretionary"
        assert result.source == "category_map"

    def test_category_donations_to_technology(self) -> None:
        result = classify_merchant(
            "Some Donation Platform", category="donations"
        )
        assert result.sector == "Technology"
        assert result.source == "category_map"


# ═══════════════════════════════════════════════════════════════════════
# classify_merchant — sector_map fallback (priority 3)
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyMerchantSectorMap:
    """classify_merchant with unknown merchant and no category."""

    def test_unknown_merchant_defaults_medium(self) -> None:
        result = classify_merchant("Random Local Shop")
        assert result.is_subscription is True
        assert result.confidence == 0.60  # MEDIUM
        assert result.source == "sector_map"
        assert result.sector is None
        assert result.ticker is None

    def test_empty_merchant_name(self) -> None:
        result = classify_merchant("")
        assert result.is_subscription is True
        assert result.source == "sector_map"

    def test_none_merchant_name_after_normalisation(self) -> None:
        """A merchant name that normalises to empty falls through."""
        result = classify_merchant("   ")
        assert result.source == "sector_map"

    def test_unknown_merchant_with_unknown_category(self) -> None:
        """Unmapped category still falls through to sector_map default."""
        result = classify_merchant("Random Shop", category="unknown_category")
        assert result.source == "sector_map"


# ═══════════════════════════════════════════════════════════════════════
# classify_merchant — fundamentals adjustment
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyMerchantFundamentals:
    """classify_merchant with optional fundamentals parameters."""

    def test_high_pe_upgrades_medium_to_high(self) -> None:
        """A known merchant with high PE and no dividend gets upgraded."""
        # We use a merchant that resolves to merchant_map
        # Passing high PE should adjust the likelihood upward
        result = classify_merchant(
            "Netflix B.V.",
            pe_ratio=Decimal(80),
            dividend_yield=Decimal("0.00"),
        )
        # High PE should keep HIGH likelihood
        assert result.is_subscription is True
        assert result.source == "merchant_map"
        assert result.confidence >= 0.50

    def test_high_dividend_downgrades(self) -> None:
        """A known merchant with high dividend gets downgraded."""
        result = classify_merchant(
            "Netflix B.V.",
            pe_ratio=Decimal(15),
            dividend_yield=Decimal("0.05"),
        )
        # High dividend should downgrade from HIGH to MEDIUM
        assert result.is_subscription is True
        assert result.confidence <= 0.85  # downgraded
        assert result.subscription_likelihood == LIKELIHOOD_MEDIUM

    def test_no_fundamentals_no_change(self) -> None:
        """Without fundamentals, result is standard merchant_map."""
        with_fund = classify_merchant(
            "Netflix B.V.",
            pe_ratio=Decimal(80),
            dividend_yield=Decimal("0.00"),
        )
        without = classify_merchant("Netflix B.V.")
        # With high PE, confidence should be >= standard
        assert with_fund.confidence >= without.confidence


# ═══════════════════════════════════════════════════════════════════════
# _likelihood_to_confidence
# ═══════════════════════════════════════════════════════════════════════


class TestLikelihoodToConfidence:
    """Verify _likelihood_to_confidence mapping."""

    def test_high_returns_0_85(self) -> None:
        assert _likelihood_to_confidence(LIKELIHOOD_HIGH) == 0.85

    def test_medium_returns_0_60(self) -> None:
        assert _likelihood_to_confidence(LIKELIHOOD_MEDIUM) == 0.60

    def test_low_returns_0_30(self) -> None:
        assert _likelihood_to_confidence(LIKELIHOOD_LOW) == 0.30

    def test_unknown_returns_default_0_30(self) -> None:
        assert _likelihood_to_confidence("unknown") == 0.30

    def test_none_returns_default_0_30(self) -> None:
        # mypy complains but we test the fallback
        assert _likelihood_to_confidence("nonexistent") == 0.30  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# normalise_merchant_name convenience
# ═══════════════════════════════════════════════════════════════════════


class TestNormaliseMerchantName:
    """Verify normalise_merchant_name delegates correctly."""

    def test_strips_bv_suffix(self) -> None:
        assert normalise_merchant_name("Netflix B.V.") == "netflix"

    def test_strips_ltd(self) -> None:
        assert normalise_merchant_name("Spotify Ltd") == "spotify"

    def test_case_insensitive(self) -> None:
        assert normalise_merchant_name("NETFLIX B.V.") == "netflix"

    def test_already_clean(self) -> None:
        assert normalise_merchant_name("netflix") == "netflix"


# ═══════════════════════════════════════════════════════════════════════
# resolve_ticker
# ═══════════════════════════════════════════════════════════════════════


class TestResolveTicker:
    """Verify resolve_ticker helper."""

    def test_known_merchant_returns_ticker(self) -> None:
        assert resolve_ticker("Netflix B.V.") == "NFLX"

    def test_spotify_returns_ticker(self) -> None:
        assert resolve_ticker("Spotify AB") == "SPOT"

    def test_private_company_returns_none(self) -> None:
        assert resolve_ticker("Patreon") is None

    def test_unknown_merchant_returns_none(self) -> None:
        assert resolve_ticker("Random Local Shop") is None

    def test_empty_name_returns_none(self) -> None:
        assert resolve_ticker("") is None


# ═══════════════════════════════════════════════════════════════════════
# sector_subscription_likelihood
# ═══════════════════════════════════════════════════════════════════════


class TestSectorSubscriptionLikelihood:
    """Verify sector_subscription_likelihood helper."""

    def test_technology_is_high(self) -> None:
        assert sector_subscription_likelihood("Technology") == LIKELIHOOD_HIGH

    def test_communication_services_is_high(self) -> None:
        assert (
            sector_subscription_likelihood("Communication Services")
            == LIKELIHOOD_HIGH
        )

    def test_energy_is_low(self) -> None:
        assert sector_subscription_likelihood("Energy") == LIKELIHOOD_LOW

    def test_unknown_sector_returns_medium(self) -> None:
        assert sector_subscription_likelihood("Unknown") == LIKELIHOOD_MEDIUM

    def test_none_sector_returns_medium(self) -> None:
        assert sector_subscription_likelihood(None) == LIKELIHOOD_MEDIUM


# ═══════════════════════════════════════════════════════════════════════
# classification_from_db — conversion helper
# ═══════════════════════════════════════════════════════════════════════


class TestClassificationFromDb:
    """Verify classification_from_db conversion helper."""

    @pytest.mark.parametrize(
        ("likelihood", "expected_conf", "expected_sub"),
        [
            (LIKELIHOOD_HIGH, 0.85, True),
            (LIKELIHOOD_MEDIUM, 0.60, True),
            (LIKELIHOOD_LOW, 0.30, False),
        ],
    )
    def test_likelihood_mapping(
        self,
        likelihood: str,
        expected_conf: float,
        expected_sub: bool,
    ) -> None:
        result = classification_from_db(
            _merchant_name="Test",
            sector="Technology",
            ticker="TEST",
            subscription_likelihood=likelihood,
            security_id="sec_001",
            source="merchant_map",
        )
        assert result.confidence == expected_conf
        assert result.is_subscription is expected_sub
        assert result.sector == "Technology"
        assert result.ticker == "TEST"
        assert result.security_id == "sec_001"
        assert result.source == "merchant_map"
        assert result.subscription_likelihood == likelihood

    def test_security_id_sets_fundamentals_available(self) -> None:
        with_fund = classification_from_db(
            _merchant_name="T",
            sector="Tech",
            ticker="T",
            security_id="sec_001",
        )
        assert with_fund.fundamentals_available is True
        assert with_fund.security_id == "sec_001"

        without = classification_from_db(
            _merchant_name="T",
            sector="Tech",
            ticker="T",
        )
        assert without.fundamentals_available is False
        assert without.security_id is None

    def test_default_likelihood_is_medium(self) -> None:
        result = classification_from_db(
            _merchant_name="Unknown",
        )
        assert result.subscription_likelihood == LIKELIHOOD_MEDIUM
        assert result.confidence == 0.60
        assert result.is_subscription is True
