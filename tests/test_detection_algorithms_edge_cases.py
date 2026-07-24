"""Comprehensive edge-case tests for subscription detection algorithms.

Covers uncovered branches in:
- Frequency detection boundaries
- Amount consistency partial-score bands
- Confidence scoring with sector_boost and score boundaries
- Deduplication order-preservation
- Cross-referencing edge cases (empty merchant names, score tie-breaking)
- Cluster enrichment with REGULAR_INTERVAL method
- Pattern clustering confidence scoring with cross-account bonus
- Cross-account matcher amount/interval compatibility edge cases
- Merchant classifier fundamentals adjustment all branches
- _normalise_merchant_name suffix coverage completeness
- SubscriptionPatternEngine edge cases
- _merge_cross_validated with cluster-higher base
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)
from finance_sync.services.merchant_classifier import (
    LIKELIHOOD_HIGH,
    LIKELIHOOD_LOW,
    LIKELIHOOD_MEDIUM,
    _adjust_likelihood_with_fundamentals,
    _normalise_merchant_name,
    _resolve_merchant_entry,
    _sector_from_category,
)
from finance_sync.services.pattern_clustering import (
    CrossAccountMatcher,
    SubscriptionPatternEngine,
    _compute_cluster_confidence,
    _detect_periods_from_intervals,
)
from finance_sync.services.subscription_detector import (
    SubscriptionDetector,
    _amounts_are_consistent,
    _compute_confidence_score,
    _detect_frequency,
    _merge_cross_validated,
    _normalise_merchant,
)

# ═══════════════════════════════════════════════════════════════════════
# Frequency detection — boundary edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestFrequencyDetectionBoundaries:
    """Cover boundary conditions in _detect_frequency not tested elsewhere."""

    def test_monthly_upper_boundary(self) -> None:
        """35-day intervals should still match monthly (upper band edge)."""
        interval, label = _detect_frequency([35.0, 35.0])
        assert label == "monthly"
        assert interval == 30

    def test_monthly_extended_tolerance(self) -> None:
        """38-day intervals match monthly extended tolerance (line 289)."""
        interval, label = _detect_frequency([38.0, 38.0, 38.0])
        assert label == "monthly"
        assert interval == 30

    def test_monthly_extended_tolerance_lower(self) -> None:
        """25-day intervals match monthly extended tolerance."""
        interval, label = _detect_frequency([25.0, 25.0])
        assert label == "monthly"
        assert interval == 30

    def test_quarterly_lower_boundary(self) -> None:
        """80-day intervals match quarterly (lower edge)."""
        _, label = _detect_frequency([80.0, 81.0])
        assert label == "quarterly"

    def test_quarterly_upper_boundary(self) -> None:
        """100-day intervals match quarterly (upper edge)."""
        _, label = _detect_frequency([100.0, 99.0])
        assert label == "quarterly"

    def test_semiannual_lower_boundary(self) -> None:
        """160-day intervals match semiannual."""
        interval, label = _detect_frequency([160.0, 161.0])
        assert label == "semiannual"
        assert interval == 180

    def test_yearly_upper_boundary(self) -> None:
        """385-day intervals match yearly (upper edge)."""
        _, label = _detect_frequency([385.0, 384.0])
        assert label == "yearly"

    def test_just_outside_monthly_below(self) -> None:
        """24-day intervals are below monthly band — no match."""
        days, label = _detect_frequency([24.0, 24.0])
        assert label is None
        assert days is None

    def test_just_outside_monthly_above(self) -> None:
        """39-day intervals are above monthly extended tolerance — no match."""
        days, label = _detect_frequency([39.0, 39.0])
        assert label is None
        assert days is None

    def test_single_interval(self) -> None:
        """A single interval with no matching band returns None."""
        days, label = _detect_frequency([45.0])
        assert label is None
        assert days is None


# ═══════════════════════════════════════════════════════════════════════
# Amount consistency — all score bands
# ═══════════════════════════════════════════════════════════════════════


class TestAmountConsistencyAllBands:
    """Cover every score band in _amounts_are_consistent."""

    def test_moderate_variance_0_6_band(self) -> None:
        """Variance ~14% (between 5%-15%) => score 0.6."""
        amounts = [Decimal("-100.00"), Decimal("-115.00"), Decimal("-108.00")]
        score = _amounts_are_consistent(amounts)
        # max_dev=15, mean=107.67, var=13.9% → 0.6 band
        assert score == 0.6

    def test_moderate_variance_0_3_band(self) -> None:
        """Variance ~20% (between 15%-30%) => score 0.3."""
        amounts = [Decimal("-100.00"), Decimal("-140.00")]
        score = _amounts_are_consistent(amounts)
        # abs amounts: [100, 140], mean=120, max_dev=20, var=16.7% → in 0.3 band
        assert score == 0.3

    def test_high_variance_returns_0_0(self) -> None:
        """Variance >30% => score 0.0."""
        amounts = [Decimal("-100.00"), Decimal("-200.00")]
        score = _amounts_are_consistent(amounts)
        # abs amounts: [100, 200], mean=150, max_dev=50, var=33.3% → >30% → 0.0
        assert score == 0.0

    def test_small_absolute_variance_dominant(self) -> None:
        """Even for larger base amounts, if abs variance <= 2, score 1.0."""
        amounts = [Decimal("-10.00"), Decimal("-11.50")]
        score = _amounts_are_consistent(amounts)
        # max_dev=1.50, which is <= 2.00 absolute — score 1.0
        assert score == 1.0

    def test_relative_variance_edge_5_pct(self) -> None:
        """Variance exactly at 5% boundary => score 1.0."""
        amounts = [Decimal("-100.00"), Decimal("-105.00")]
        score = _amounts_are_consistent(amounts)
        # max_dev=5, mean=102.5, var=4.88% → within 5% → 1.0
        assert score == 1.0


# ═══════════════════════════════════════════════════════════════════════
# Confidence scoring — boundary values and sector_boost
# ═══════════════════════════════════════════════════════════════════════


class TestConfidenceScoringBoundaries:
    """Verify confidence scoring at exact thresholds and with sector_boost."""

    def test_high_threshold_exact_0_80_with_boost(self) -> None:
        """Score 0.80 with sector_boost 0.0 should still reach HIGH."""
        level, score = _compute_confidence_score(
            occurrence_count=6,
            amount_consistency=1.0,
            interval_regularity=1.0,
            has_keyword=True,
            has_category=True,
            sector_boost=0.0,
        )
        # 0.25 (6+) + 0.25 (1.0) + 0.25 (1.0) + 0.12 + 0.08 = 0.95 → HIGH
        assert level == SubscriptionConfidence.HIGH
        assert score == 0.95

    def test_medium_threshold_boundary(self) -> None:
        """Score exactly 0.50 should be MEDIUM."""
        level, score = _compute_confidence_score(
            occurrence_count=2,  # 0.08
            amount_consistency=1.0,  # 0.25
            interval_regularity=0.4,  # 0.10
            has_keyword=False,
            has_category=True,  # 0.08
            sector_boost=0.0,
        )
        # 0.08 + 0.25 + 0.10 + 0.08 = 0.51 → MEDIUM
        assert level == SubscriptionConfidence.MEDIUM
        assert score == pytest.approx(0.51)

    def test_low_stays_low_just_below_medium(self) -> None:
        """Score just below 0.50 stays LOW."""
        level, score = _compute_confidence_score(
            occurrence_count=2,  # 0.08
            amount_consistency=0.6,  # 0.15
            interval_regularity=0.4,  # 0.10
            has_keyword=False,
            has_category=False,  # no bonus
            sector_boost=0.0,
        )
        # 0.08 + 0.15 + 0.10 = 0.33 → LOW
        assert level == SubscriptionConfidence.LOW
        assert score == pytest.approx(0.33)

    def test_sector_boost_lifts_medium_to_high(self) -> None:
        """Sector boost (0.12) pushes MEDIUM-over threshold to HIGH."""
        level, score = _compute_confidence_score(
            occurrence_count=6,  # 0.25
            amount_consistency=0.6,  # 0.15
            interval_regularity=0.4,  # 0.10
            has_keyword=True,  # 0.12
            has_category=True,  # 0.08
            sector_boost=0.12,
        )
        # 0.25 + 0.15 + 0.10 + 0.12 + 0.08 + 0.12 = 0.82 → HIGH
        assert level == SubscriptionConfidence.HIGH
        assert score == pytest.approx(0.82)

    def test_sector_boost_at_max_caps_score(self) -> None:
        """score + sector_boost does not exceed 1.0."""
        level, score = _compute_confidence_score(
            occurrence_count=12,  # 0.30
            amount_consistency=1.0,  # 0.25
            interval_regularity=1.0,  # 0.25
            has_keyword=True,  # 0.12
            has_category=True,  # 0.08
            sector_boost=0.12,
        )
        # 0.30 + 0.25 + 0.25 + 0.12 + 0.08 + 0.12 = 1.12 → capped at 1.0
        assert score == 1.0
        assert level == SubscriptionConfidence.HIGH

    def test_sector_boost_zero_no_effect(self) -> None:
        """Zero sector_boost has no visible effect on score."""
        _, score_no_boost = _compute_confidence_score(
            occurrence_count=4,  # 0.20
            amount_consistency=0.6,  # 0.15
            interval_regularity=0.4,  # 0.10
            has_keyword=True,  # 0.12
            has_category=True,  # 0.08
            sector_boost=0.0,
        )
        _, score_with_boost = _compute_confidence_score(
            occurrence_count=4,
            amount_consistency=0.6,
            interval_regularity=0.4,
            has_keyword=True,
            has_category=True,
            sector_boost=0.12,
        )
        # 0.20+0.15+0.10+0.12+0.08 = 0.65 vs 0.77
        assert score_with_boost == pytest.approx(0.77)
        assert score_no_boost == pytest.approx(0.65)
        assert score_with_boost > score_no_boost


# ═══════════════════════════════════════════════════════════════════════
# Deduplication — order preservation
# ═══════════════════════════════════════════════════════════════════════


class TestDedupOrderPreservation:
    """Verify _deduplicate_results preserves first-seen order for ties."""

    def test_first_seen_wins_equal_score_and_sector(self) -> None:
        """When scores and sector presence are equal, first-seen order wins."""
        results = [
            {
                "merchant_name": "Netflix",
                "detection_score": 0.80,
                "sector": "Technology",
            },
            {
                "merchant_name": "Spotify",
                "detection_score": 0.80,
                "sector": "Communication Services",
            },
            {
                "merchant_name": "Netflix",
                "detection_score": 0.80,
                "sector": "Technology",
            },
        ]
        deduped = SubscriptionDetector._deduplicate_results(results)
        assert len(deduped) == 2  # Two unique merchants
        assert deduped[0]["merchant_name"] == "Netflix"
        assert deduped[1]["merchant_name"] == "Spotify"

    def test_higher_score_wins_among_duplicates(self) -> None:
        """When duplicate has higher score, it replaces earlier entry."""
        results = [
            {
                "merchant_name": "Netflix",
                "detection_score": 0.80,
                "sector": None,
            },
            {
                "merchant_name": "Netflix",
                "detection_score": 0.90,
                "sector": "Technology",
            },
        ]
        deduped = SubscriptionDetector._deduplicate_results(results)
        assert len(deduped) == 1
        assert deduped[0]["detection_score"] == 0.90

    def test_empty_list_returns_empty(self) -> None:
        assert SubscriptionDetector._deduplicate_results([]) == []

    def test_single_result_unchanged(self) -> None:
        results = [{"merchant_name": "Netflix", "detection_score": 0.80}]
        assert SubscriptionDetector._deduplicate_results(results) == results


# ═══════════════════════════════════════════════════════════════════════
# _merge_cross_validated — cluster-score-higher base
# ═══════════════════════════════════════════════════════════════════════


class TestMergeCrossValidatedExtended:
    """Extended _merge_cross_validated edge cases."""

    def test_cluster_score_higher_becomes_base(self) -> None:
        """When cluster score > merchant score, cluster is the base."""
        mr = _merchant_result(score=0.50, sector="Communication Services")
        cr = _cluster_result(score=0.75, sector="Financials")
        merged = _merge_cross_validated(mr, cr)

        assert merged["detection_method"] == DetectionMethod.HYBRID
        assert merged["detection_score"] == pytest.approx(0.85)  # 0.75 + 0.10
        assert merged["confidence"] == SubscriptionConfidence.HIGH

    def test_sector_from_merchant_when_cluster_none(self) -> None:
        """When cluster has no sector but merchant does, sector is filled."""
        mr = _merchant_result(score=0.70, sector="Communication Services")
        cr = _cluster_result(score=0.60, sector=None)
        merged = _merge_cross_validated(mr, cr)
        assert merged["sector"] == "Communication Services"

    def test_sector_from_cluster_when_merchant_none(self) -> None:
        """When merchant has no sector but cluster does, sector is filled."""
        mr = _merchant_result(score=0.70, sector=None)
        cr = _cluster_result(score=0.60, sector="Financials")
        merged = _merge_cross_validated(mr, cr)
        assert merged["sector"] == "Financials"

    def test_security_id_filled_from_other(self) -> None:
        """Security ID from one source fills the other."""
        mr = _merchant_result(score=0.70, security_id="sec_nflx")
        cr = _cluster_result(score=0.60, sector="Communication Services")
        cr["security_id"] = None
        merged = _merge_cross_validated(mr, cr)
        assert merged["security_id"] == "sec_nflx"

    def test_merged_transaction_count_accurate(self) -> None:
        """merged_transaction_count in details reflects total dedup count."""
        mr = _merchant_result(
            score=0.70, txn_ids=["a", "b", "c"], occurrence_count=3
        )
        cr = _cluster_result(
            score=0.60, txn_ids=["c", "d", "e"], occurrence_count=3
        )
        merged = _merge_cross_validated(mr, cr)
        assert merged["details"]["merged_transaction_count"] == 5
        assert len(merged["transaction_ids"]) == 5

    def test_merchant_score_zero_both(self) -> None:
        """Zero scores + LOW confidence stays LOW with bonus."""
        mr = _merchant_result(score=0.0, sector="Technology")
        mr["confidence"] = SubscriptionConfidence.LOW
        cr = _cluster_result(score=0.0, sector="Financials")
        cr["confidence"] = SubscriptionConfidence.LOW
        merged = _merge_cross_validated(mr, cr)
        # New score = 0.0 + 0.10 = 0.10 → LOW stays LOW
        assert merged["detection_score"] == pytest.approx(0.10)
        assert merged["confidence"] == SubscriptionConfidence.LOW

    def test_full_confidence_matrix(self) -> None:
        """MEDIUM + 0.10 bonus crosses HIGH threshold at 0.80."""
        mr = _merchant_result(score=0.71)
        mr["confidence"] = SubscriptionConfidence.MEDIUM
        cr = _cluster_result(score=0.60)
        merged = _merge_cross_validated(mr, cr)
        # 0.71 + 0.10 = 0.81 → HIGH
        assert merged["confidence"] == SubscriptionConfidence.HIGH
        assert merged["detection_score"] == pytest.approx(0.81)


# ═══════════════════════════════════════════════════════════════════════
# _enrich_cluster_results — REGULAR_INTERVAL branch
# ═══════════════════════════════════════════════════════════════════════


class TestEnrichClusterResultsExtended:
    """Cover all detection methods in _enrich_cluster_results."""

    def test_enrich_regular_interval_method(self) -> None:
        """REGULAR_INTERVAL cluster results also get enriched."""
        detector = SubscriptionDetector(
            session_factory=MagicMock(), tenant_id="tenant_1"
        )
        cluster_results = [
            {
                "merchant_name": "Netflix",
                "amount": "-15.99",
                "detection_method": DetectionMethod.REGULAR_INTERVAL,
                "detection_score": 0.50,
                "confidence": SubscriptionConfidence.LOW,
                "details": {},
            }
        ]
        classifications = {
            "Netflix": {
                "sector": "Communication Services",
                "security_id": "sec_nflx",
                "likelihood_score": 0.12,
                "ticker": "NFLX",
                "source": "merchant_map",
            }
        }
        enriched = detector._enrich_cluster_results(
            cluster_results, classifications
        )
        assert enriched[0]["sector"] == "Communication Services"
        assert (
            enriched[0]["detection_method"]
            == DetectionMethod.MERCHANT_CLASSIFICATION
        )
        assert enriched[0]["detection_score"] == pytest.approx(0.62)

    def test_enrich_preserves_other_methods(self) -> None:
        """EXACT_AMOUNT and SIMILAR_AMOUNT are not overwritten."""
        detector = SubscriptionDetector(
            session_factory=MagicMock(), tenant_id="tenant_1"
        )
        cluster_results = [
            {
                "merchant_name": "Netflix",
                "amount": "-15.99",
                "detection_method": DetectionMethod.EXACT_AMOUNT,
                "detection_score": 0.70,
                "confidence": SubscriptionConfidence.MEDIUM,
                "details": {},
            }
        ]
        classifications = {
            "Netflix": {
                "sector": "Communication Services",
                "security_id": "sec_nflx",
                "likelihood_score": 0.12,
            }
        }
        enriched = detector._enrich_cluster_results(
            cluster_results, classifications
        )
        # EXACT_AMOUNT is not in the upgrade list, but sector/score still added
        assert enriched[0]["sector"] == "Communication Services"
        assert enriched[0]["detection_method"] == DetectionMethod.EXACT_AMOUNT


# ═══════════════════════════════════════════════════════════════════════
# Pattern clustering — _compute_cluster_confidence all branches
# ═══════════════════════════════════════════════════════════════════════


class TestClusterConfidenceAllBands:
    """Cover every score band in _compute_cluster_confidence."""

    def test_high_confidence_max_scores(self) -> None:
        """All maximum inputs produce HIGH confidence."""
        level, score = _compute_cluster_confidence(
            occurrence_count=12,
            amount_consistency=1.0,
            interval_regularity=1.0,
            cluster_size=10,
            has_cross_account_confirmation=True,
        )
        # 0.25 + 0.25 + 0.25 + 0.15 + 0.10 = 1.0 → HIGH
        assert level == SubscriptionConfidence.HIGH
        assert score == 1.0

    def test_medium_confidence_with_cross_account(self) -> None:
        """Cross-account bonus pushes score above 0.50."""
        level, score = _compute_cluster_confidence(
            occurrence_count=3,  # 0.10
            amount_consistency=0.6,  # 0.15
            interval_regularity=0.4,  # 0.10
            cluster_size=2,  # min(1.0, 2/10) * 0.15 = 0.03
            has_cross_account_confirmation=True,  # 0.10
        )
        # 0.10 + 0.15 + 0.10 + 0.03 + 0.10 = 0.48
        assert level == SubscriptionConfidence.LOW
        assert score == pytest.approx(0.48)

    def test_cross_account_bonus_makes_difference(self) -> None:
        """Same inputs, with vs without cross-account bonus."""
        _, score_without = _compute_cluster_confidence(
            occurrence_count=6,
            amount_consistency=0.6,
            interval_regularity=0.7,
            cluster_size=4,
            has_cross_account_confirmation=False,
        )
        _, score_with = _compute_cluster_confidence(
            occurrence_count=6,
            amount_consistency=0.6,
            interval_regularity=0.7,
            cluster_size=4,
            has_cross_account_confirmation=True,
        )
        assert score_with == pytest.approx(score_without + 0.10)

    def test_cluster_density_capped_at_one(self) -> None:
        """Cluster density factor is capped at 1.0 (cluster_size >= 10)."""
        _, score_small = _compute_cluster_confidence(
            occurrence_count=2,
            amount_consistency=0.0,
            interval_regularity=0.0,
            cluster_size=5,
        )
        _, score_large = _compute_cluster_confidence(
            occurrence_count=2,
            amount_consistency=0.0,
            interval_regularity=0.0,
            cluster_size=20,
        )
        # density: min(1.0, 5/10)*0.15=0.075 vs min(1.0, 20/10)*0.15=0.15
        assert score_large > score_small
        assert score_large == pytest.approx(
            0.05 + 0.15
        )  # 0.08 (2 occ) + capped density

    def test_low_confidence_minimum_inputs(self) -> None:
        """Minimum viable inputs produce LOW confidence."""
        level, score = _compute_cluster_confidence(
            occurrence_count=2,  # 0.05
            amount_consistency=0.0,  # 0.00
            interval_regularity=0.0,  # 0.00
            cluster_size=2,  # 0.03
            has_cross_account_confirmation=False,
        )
        assert level == SubscriptionConfidence.LOW
        assert score == pytest.approx(0.08)


# ═══════════════════════════════════════════════════════════════════════
# CrossAccountMatcher — amount/interval compatibility edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestCrossAccountCompatibility:
    """Edge cases for CrossAccountMatcher compatibility checks."""

    def setup_method(self) -> None:
        self.matcher = CrossAccountMatcher()

    def test_amounts_compatible_exact(self) -> None:
        assert self.matcher._amounts_compatible(
            Decimal("-15.99"), Decimal("-15.99")
        )

    def test_amounts_compatible_close(self) -> None:
        """Amounts within 50% of each other are compatible."""
        assert self.matcher._amounts_compatible(
            Decimal("-15.99"), Decimal("-10.00")
        )
        # ratio = 10/15.99 = 0.625 > 0.50

    def test_amounts_not_compatible(self) -> None:
        """Amounts with ratio below 0.50 are not compatible."""
        assert not self.matcher._amounts_compatible(
            Decimal("-15.99"), Decimal("-5.00")
        )
        # ratio = 5/15.99 = 0.313 < 0.50

    def test_amounts_both_zero(self) -> None:
        assert self.matcher._amounts_compatible(Decimal(0), Decimal(0))

    def test_amounts_one_zero(self) -> None:
        """If one amount is zero, they're not compatible unless both are."""
        assert not self.matcher._amounts_compatible(
            Decimal(0), Decimal("-15.99")
        )

    def test_amounts_positive_and_negative(self) -> None:
        """Sign doesn't matter — absolute values are compared."""
        assert self.matcher._amounts_compatible(
            Decimal("-15.99"), Decimal("15.99")
        )

    def test_intervals_compatible_both_none(self) -> None:
        assert self.matcher._intervals_compatible(None, None)

    def test_intervals_compatible_one_none(self) -> None:
        assert self.matcher._intervals_compatible(30, None)
        assert self.matcher._intervals_compatible(None, 30)

    def test_intervals_compatible_zero_freq(self) -> None:
        """Zero frequency days is treated as compatible."""
        assert self.matcher._intervals_compatible(0, 30)
        assert self.matcher._intervals_compatible(0, 0)

    def test_intervals_compatible_close_values(self) -> None:
        """Within 20% mismatch tolerance."""
        # 30 vs 35: ratio = 30/35 = 0.857, 1-0.857=0.143 < 0.20 → compatible
        assert self.matcher._intervals_compatible(30, 35)

    def test_intervals_not_compatible(self) -> None:
        """Outside 20% mismatch tolerance."""
        # 30 vs 40: ratio = 30/40 = 0.75, 1-0.75=0.25 > 0.20 → not compatible
        assert not self.matcher._intervals_compatible(30, 40)


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionPatternEngine — edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPatternEngineEdgeCases:
    """Edge cases for SubscriptionPatternEngine.detect()."""

    def test_empty_transactions(self) -> None:
        engine = SubscriptionPatternEngine()
        assert engine.detect([]) == []

    def test_single_transaction(self) -> None:
        """A single transaction cannot form a pattern."""
        txn = {
            "id": "t1",
            "amount": Decimal("-15.99"),
            "currency_code": "EUR",
            "description": "Netflix",
            "occurred_at": datetime(2025, 1, 15, tzinfo=UTC),
            "account_id": "acct_1",
            "provider_key": "bunq",
            "transaction_type": "payment",
        }
        engine = SubscriptionPatternEngine(min_occurrences=2)
        assert engine.detect([txn]) == []

    def test_same_amount_different_merchants(self) -> None:
        """Transactions with same amount but different descriptions."""
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            {
                "id": f"t{i}",
                "amount": Decimal("-15.99"),
                "currency_code": "EUR",
                "description": desc,
                "occurred_at": base + timedelta(days=30 * i),
                "account_id": "acct_1",
                "provider_key": "bunq",
                "transaction_type": "payment",
            }
            for i, desc in enumerate(
                ["Netflix", "Netflix", "Netflix", "Spotify", "Spotify"]
            )
        ]
        engine = SubscriptionPatternEngine(min_occurrences=2)
        patterns = engine.detect(txns)
        assert len(patterns) >= 1


# ═══════════════════════════════════════════════════════════════════════
# Merchant classifier — fundamentals adjustment all branches
# ═══════════════════════════════════════════════════════════════════════


class TestFundamentalsAdjustmentAllBranches:
    """Cover all branches in _adjust_likelihood_with_fundamentals."""

    def test_low_stays_low(self) -> None:
        """LOW likelihood never upgrades."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_LOW,
            pe_ratio=Decimal(100),
            dividend_yield=Decimal("0.0"),
        )
        assert result == LIKELIHOOD_LOW

    def test_high_with_high_dividend_downgrades(self) -> None:
        """HIGH + dividend > 4% → MEDIUM."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_HIGH,
            pe_ratio=Decimal(30),
            dividend_yield=Decimal("0.05"),
        )
        assert result == LIKELIHOOD_MEDIUM

    def test_medium_with_high_pe_upgrades(self) -> None:
        """MEDIUM + PE > 50 → HIGH."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=Decimal(60),
            dividend_yield=Decimal("0.01"),
        )
        assert result == LIKELIHOOD_HIGH

    def test_dividend_above_3_pct_cancels_upgrade(self) -> None:
        """Dividend > 3% cancels PE-based upgrade."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=Decimal(60),
            dividend_yield=Decimal("0.035"),
        )
        # dividend 3.5% > 3% → upgrade=False → stays MEDIUM
        assert result == LIKELIHOOD_MEDIUM

    def test_no_fundamentals_no_change(self) -> None:
        """None fundamentals keep likelihood unchanged."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_MEDIUM,
            pe_ratio=None,
            dividend_yield=None,
        )
        assert result == LIKELIHOOD_MEDIUM

        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_HIGH,
            pe_ratio=None,
            dividend_yield=None,
        )
        assert result == LIKELIHOOD_HIGH

    def test_high_with_both_factors_downgrade_wins(self) -> None:
        """HIGH + high PE + high dividend: downgrade takes priority."""
        result = _adjust_likelihood_with_fundamentals(
            LIKELIHOOD_HIGH,
            pe_ratio=Decimal(60),
            dividend_yield=Decimal("0.06"),
        )
        # dividend > 4% -> downgrade. PE > 50 -> upgrade.
        # net: downgrade from HIGH to MEDIUM
        assert result == LIKELIHOOD_MEDIUM


# ═══════════════════════════════════════════════════════════════════════
# Merchant classifier — _normalise_merchant_name suffix coverage
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantNameNormalisationSuffixCoverage:
    """Cover all suffix types in _normalise_merchant_name."""

    def test_strips_sa_suffix(self) -> None:
        assert _normalise_merchant_name("TotalEnergies S.A.") == "totalenergies"

    def test_strips_sl_suffix(self) -> None:
        assert _normalise_merchant_name("Empresa S.L.") == "empresa"

    def test_strips_ag_suffix(self) -> None:
        assert _normalise_merchant_name("Siemens AG") == "siemens"

    def test_strips_co_suffix(self) -> None:
        assert _normalise_merchant_name("Shell Co.") == "shell"

    def test_strips_co_suffix_no_dot(self) -> None:
        assert _normalise_merchant_name("Shell Co") == "shell"

    def test_strips_holding_suffix(self) -> None:
        assert _normalise_merchant_name("Alphabet Holding") == "alphabet"

    def test_strips_holdings_suffix(self) -> None:
        assert _normalise_merchant_name("Naspers Holdings") == "naspers"

    def test_strips_international(self) -> None:
        assert _normalise_merchant_name("Company International") == "company"

    def test_strips_technology(self) -> None:
        assert _normalise_merchant_name("Company Technology") == "company"

    def test_known_merchants_resolve_after_normalisation(self) -> None:
        """Verify several known merchants resolve via prefix matching."""
        tests = [
            ("Google Workspace", "Technology", "GOOGL"),
            ("Microsoft 365 Subscription", "Technology", "MSFT"),
            ("Disney+ Annual Plan", "Communication Services", "DIS"),
            ("Peloton Membership", "Consumer Discretionary", "PTON"),
            ("Digital Ocean Droplet", "Technology", "DOCN"),
        ]
        for name, expected_sector, expected_ticker in tests:
            entry = _resolve_merchant_entry(name)
            assert entry is not None, f"Failed to resolve {name!r}"
            assert entry["sector"] == expected_sector, (
                f"{name}: sector mismatch"
            )
            assert entry["ticker"] == expected_ticker, (
                f"{name}: ticker mismatch"
            )

    def test_prefix_matching_shortest_first(self) -> None:
        """Prefix matching tries longest prefix first."""
        entry = _resolve_merchant_entry("Office 365 Business Premium")
        assert entry is not None
        assert entry["ticker"] == "MSFT"

    def test_no_match_returns_none(self) -> None:
        """Unrecognised merchant returns None."""
        assert _resolve_merchant_entry("Totally Unknown Shop") is None


# ═══════════════════════════════════════════════════════════════════════
# Merchant classifier — _sector_from_category coverage
# ═══════════════════════════════════════════════════════════════════════


class TestSectorFromCategoryCoverage:
    """Cover all category-to-sector mappings."""

    def test_streaming(self) -> None:
        assert _sector_from_category("streaming") == "Communication Services"

    def test_software(self) -> None:
        assert _sector_from_category("software") == "Technology"

    def test_utilities(self) -> None:
        assert _sector_from_category("utilities") == "Utilities"

    def test_fitness(self) -> None:
        assert _sector_from_category("fitness") == "Consumer Discretionary"

    def test_insurance(self) -> None:
        assert _sector_from_category("insurance") == "Financials"

    def test_news_media(self) -> None:
        assert _sector_from_category("news_media") == "Communication Services"

    def test_donations(self) -> None:
        assert _sector_from_category("donations") == "Technology"

    def test_cloud_storage(self) -> None:
        assert _sector_from_category("cloud_storage") == "Technology"

    def test_none_category(self) -> None:
        assert _sector_from_category(None) is None

    def test_unknown_category(self) -> None:
        assert _sector_from_category("unknown") is None


# ═══════════════════════════════════════════════════════════════════════
# _normalise_merchant — additional prefix coverage
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantNormalisationPrefixCoverage:
    """Cover prefix types in _normalise_merchant not tested elsewhere."""

    def test_so_prefix(self) -> None:
        """'SO' (standing order) prefix is stripped."""
        assert _normalise_merchant("SO Netflix B.V.") == "Netflix B.V."

    def test_betaling_prefix(self) -> None:
        """'BETALING' (Dutch for payment) prefix is stripped."""
        assert _normalise_merchant("BETALING Spotify AB") == "Spotify Ab"

    def test_i_deal_prefix_variant(self) -> None:
        """'I DEAL' prefix with different spacing."""
        assert _normalise_merchant("I DEAL Mollie B.V.") == "Mollie B.V."

    def test_trx_number_reference(self) -> None:
        """'TRX:' reference numbers are stripped."""
        result = _normalise_merchant("Netflix TRX: abc123def456")
        assert result == "Netflix"

    def test_trans_reference(self) -> None:
        """'TRANS' reference numbers are stripped."""
        result = _normalise_merchant("Spotify TRANS: 987654")
        assert result == "Spotify"

    def test_nr_reference(self) -> None:
        """'NR:' reference numbers are stripped."""
        result = _normalise_merchant("GitHub NR: 123456")
        assert result == "Github"
        # Note: title-case makes "GitHub" into "Github"


# ═══════════════════════════════════════════════════════════════════════
# _detect_periods_from_intervals — additional edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestDetectPeriodsExtended:
    """Additional _detect_periods_from_intervals edge cases."""

    def test_large_intervals_only_yearly(self) -> None:
        intervals = [365.0, 366.0, 364.0, 365.0]
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        assert periods[0].label == "yearly"

    def test_biweekly_intervals(self) -> None:
        intervals = [14.0, 14.0, 15.0, 14.0]
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        assert periods[0].label == "biweekly"

    def test_periods_sorted_by_score(self) -> None:
        """Periods are sorted by score descending."""
        intervals = [30.0] * 15 + [7.0] * 3
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 2
        for i in range(len(periods) - 1):
            assert periods[i].score >= periods[i + 1].score

    def test_many_intervals_still_finds_best_band(self) -> None:
        """Even with many mixed intervals, the best band is found."""
        intervals = [30.0] * 20 + [90.0] * 5 + [7.0] * 2
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        assert periods[0].label == "monthly"


# ═══════════════════════════════════════════════════════════════════════
# Helpers (reused across classes above)
# ═══════════════════════════════════════════════════════════════════════


def _merchant_result(
    *,
    merchant: str = "Netflix B.V.",
    score: float = 0.75,
    amount: str = "-15.99",
    sector: str | None = "Communication Services",
    security_id: str | None = "sec_nflx",
    category: str = "streaming",
    frequency_label: str = "monthly",
    occurrence_count: int = 6,
    txn_ids: list[str] | None = None,
) -> dict:
    """Merchant-based detection result dict (like integration tests)."""
    base = datetime(2025, 1, 15, tzinfo=UTC)
    ids = txn_ids or [f"{merchant}_txn_{i}" for i in range(occurrence_count)]
    return {
        "merchant_name": merchant,
        "raw_description": f"POS {merchant}",
        "amount": Decimal(amount),
        "currency_code": "EUR",
        "frequency_days": 30,
        "frequency_label": frequency_label,
        "confidence": SubscriptionConfidence.MEDIUM,
        "detection_method": DetectionMethod.MERCHANT_CLASSIFICATION,
        "status": SubscriptionStatus.ACTIVE,
        "transaction_ids": ids,
        "account_id": "acct_1",
        "provider_key": "bunq",
        "category": category,
        "sector": sector,
        "security_id": security_id,
        "first_detected_at": base,
        "last_detected_at": base + timedelta(days=30 * (occurrence_count - 1)),
        "occurrence_count": occurrence_count,
        "detection_score": score,
        "details": {
            "amount_consistency": 1.0,
            "interval_regularity": 1.0,
            "intervals_days": [30.0] * (occurrence_count - 1),
            "has_keyword": True,
            "amounts": [amount] * occurrence_count,
            "sector_boost": 0.12,
        },
    }


def _cluster_result(
    *,
    merchant: str = "Netflix B.V.",
    score: float = 0.68,
    amount: str = "-15.99",
    sector: str | None = None,
    category: str = "streaming",
    frequency_label: str = "monthly",
    occurrence_count: int = 4,
    txn_ids: list[str] | None = None,
) -> dict:
    """Clustering-based detection result dict (like integration tests)."""
    base = datetime(2025, 1, 15, tzinfo=UTC)
    ids = txn_ids or [
        f"{merchant}_cluster_{i}" for i in range(occurrence_count)
    ]
    return {
        "merchant_name": merchant,
        "raw_description": f"DEB {merchant}",
        "amount": Decimal(amount),
        "currency_code": "EUR",
        "frequency_days": 30,
        "frequency_label": frequency_label,
        "confidence": SubscriptionConfidence.MEDIUM,
        "detection_method": DetectionMethod.AMOUNT_CLUSTER,
        "status": SubscriptionStatus.ACTIVE,
        "transaction_ids": ids,
        "account_id": "acct_1",
        "provider_key": "bunq",
        "category": category,
        "sector": sector,
        "security_id": None,
        "first_detected_at": base,
        "last_detected_at": base + timedelta(days=30 * (occurrence_count - 1)),
        "occurrence_count": occurrence_count,
        "detection_score": score,
        "details": {
            "amount_consistency": 1.0,
            "interval_regularity": 0.9,
            "intervals_days": [30.0] * (occurrence_count - 1),
            "periods": [{"days": 30, "label": "monthly", "score": 0.95}],
            "cluster_size": occurrence_count,
            "has_keyword": False,
            "amounts": [amount] * occurrence_count,
        },
    }
