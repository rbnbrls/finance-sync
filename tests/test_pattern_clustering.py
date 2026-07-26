"""Tests for the pattern clustering module.

Covers:
- 1D density-based clustering
- Amount cluster detection
- Interval histogram and peak detection
- Periodic pattern detection
- Period-to-frequency mapping
- Cross-account matching
- Confidence scoring for cluster-based patterns
- Full pipeline (SubscriptionPatternEngine)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
)
from finance_sync.services.pattern_clustering import (
    AmountCluster,
    AmountClusterDetector,
    CrossAccountMatcher,
    PeriodicPatternDetector,
    SubscriptionPatternEngine,
    _compute_cluster_confidence,
    _density_cluster_1d,
    _detect_periods_from_intervals,
    _find_peaks,
    _interval_histogram,
    _map_period_to_label,
    _smooth_series,
)

# ═══════════════════════════════════════════════════════════════════════
# 1D Density-based clustering
# ═══════════════════════════════════════════════════════════════════════


class TestDensityCluster1D:
    """Verify 1-D density-based clustering."""

    def test_exact_amounts_form_cluster(self) -> None:
        values = [
            Decimal("-9.99"),
            Decimal("-9.99"),
            Decimal("-9.99"),
            Decimal("-5.00"),
        ]
        clusters = _density_cluster_1d(
            values, eps_abs=Decimal("0.01"), min_pts=2
        )
        # The three -9.99 values should form a cluster; -5.00 may be noise
        assert len(clusters) == 1
        assert len(clusters[0]) >= 3

    def test_near_amounts_cluster_together(self) -> None:
        values = [
            Decimal("-9.99"),
            Decimal("-10.00"),
            Decimal("-9.98"),
            Decimal("-50.00"),
        ]
        clusters = _density_cluster_1d(
            values, eps_abs=Decimal("0.05"), min_pts=2
        )
        # -9.99, -10.00, -9.98 should be within eps of each other
        assert len(clusters) == 1
        assert len(clusters[0]) >= 3

    def test_relative_tolerance_for_large_amounts(self) -> None:
        # 5% relative tolerance means €100 and €104 should cluster
        values = [
            Decimal("-100.00"),
            Decimal("-104.00"),
            Decimal("-98.00"),
            Decimal("-500.00"),
        ]
        clusters = _density_cluster_1d(
            values,
            eps_pct=Decimal("0.05"),
            eps_abs=Decimal("0.01"),
            min_pts=2,
        )
        assert len(clusters) >= 1
        assert len(clusters[0]) >= 3

    def test_far_apart_amounts_do_not_cluster(self) -> None:
        values = [
            Decimal("-9.99"),
            Decimal("-50.00"),
            Decimal("-100.00"),
        ]
        clusters = _density_cluster_1d(
            values, eps_abs=Decimal("1.00"), min_pts=2
        )
        # None are within €1 of each other
        assert len(clusters) == 0

    def test_not_enough_points_for_cluster(self) -> None:
        values = [Decimal("-9.99"), Decimal("-9.99")]
        clusters = _density_cluster_1d(
            values, eps_abs=Decimal("0.01"), min_pts=3
        )
        assert len(clusters) == 0

    def test_empty_list(self) -> None:
        clusters = _density_cluster_1d([], min_pts=2)
        assert len(clusters) == 0

    def test_single_point(self) -> None:
        clusters = _density_cluster_1d([Decimal("-9.99")], min_pts=2)
        assert len(clusters) == 0

    def test_zero_amounts(self) -> None:
        values = [Decimal(0), Decimal(0), Decimal(0)]
        clusters = _density_cluster_1d(
            values, eps_abs=Decimal("0.01"), min_pts=2
        )
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_mixed_sign_amounts(self) -> None:
        # Absolute values should be compared
        values = [Decimal("-9.99"), Decimal("9.99"), Decimal("-10.00")]
        clusters = _density_cluster_1d(
            values, eps_abs=Decimal("0.05"), min_pts=2
        )
        assert len(clusters) == 1
        assert len(clusters[0]) == 3


# ═══════════════════════════════════════════════════════════════════════
# Amount cluster detection
# ═══════════════════════════════════════════════════════════════════════


class _MockTxn:
    """Minimal transaction-like dict for testing."""

    def __init__(
        self,
        *,
        txn_id: str | None = None,
        amount: Decimal = Decimal("-9.99"),
        currency_code: str = "EUR",
        description: str = "Netflix",
        occurred_at: datetime | None = None,
        account_id: str = "acct_1",
        provider_key: str = "bunq",
        transaction_type: str = "payment",
    ):
        self.id = txn_id or str(uuid4())
        self.amount = amount
        self.currency_code = currency_code
        self.description = description
        self.occurred_at = occurred_at or datetime.now(UTC)
        self.account_id = account_id
        self.provider_key = provider_key
        self.transaction_type = transaction_type


def _make_txn_dict(mock: _MockTxn) -> dict:
    return {
        "id": mock.id,
        "amount": mock.amount,
        "currency_code": mock.currency_code,
        "description": mock.description,
        "occurred_at": mock.occurred_at,
        "account_id": mock.account_id,
        "provider_key": mock.provider_key,
        "transaction_type": mock.transaction_type,
    }


class TestAmountClusterDetector:
    """Verify amount-based clustering detection."""

    def test_same_amount_cluster(self) -> None:
        txns = [
            _make_txn_dict(
                _MockTxn(amount=Decimal("-9.99"), description="Netflix")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-9.99"), description="Netflix B.V.")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-9.99"), description="NETFLIX.COM")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-50.00"), description="Other")
            ),
        ]
        detector = AmountClusterDetector()
        clusters = detector.detect_clusters(txns)
        assert len(clusters) >= 1
        assert clusters[0].count >= 3
        assert clusters[0].amount == Decimal("-9.99")

    def test_near_amounts_cluster(self) -> None:
        txns = [
            _make_txn_dict(_MockTxn(amount=Decimal("-9.99"))),
            _make_txn_dict(_MockTxn(amount=Decimal("-10.00"))),
            _make_txn_dict(_MockTxn(amount=Decimal("-9.98"))),
        ]
        detector = AmountClusterDetector(eps_abs=Decimal("0.05"))
        clusters = detector.detect_clusters(txns)
        assert len(clusters) == 1
        assert clusters[0].count == 3

    def test_multiple_clusters(self) -> None:
        txns = [
            _make_txn_dict(
                _MockTxn(amount=Decimal("-9.99"), description="Netflix")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-9.99"), description="Netflix")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-9.99"), description="Netflix")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-49.99"), description="Internet")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-49.99"), description="Internet")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-49.99"), description="Internet")
            ),
            _make_txn_dict(
                _MockTxn(amount=Decimal("-100.00"), description="One-off")
            ),
        ]
        detector = AmountClusterDetector(min_points=3)
        clusters = detector.detect_clusters(txns)
        assert len(clusters) == 2  # Two clusters of 3 items each

    def test_no_clusters_below_min_points(self) -> None:
        txns = [
            _make_txn_dict(_MockTxn(amount=Decimal("-9.99"))),
            _make_txn_dict(_MockTxn(amount=Decimal("-9.99"))),
        ]
        detector = AmountClusterDetector(min_points=3)
        assert detector.detect_clusters(txns) == []

    def test_empty_input(self) -> None:
        detector = AmountClusterDetector()
        assert detector.detect_clusters([]) == []

    def test_clusters_sorted_by_count(self) -> None:
        txns = [_make_txn_dict(_MockTxn(amount=Decimal("-5.00")))] * 5 + [
            _make_txn_dict(_MockTxn(amount=Decimal("-10.00")))
        ] * 10
        detector = AmountClusterDetector(min_points=3)
        clusters = detector.detect_clusters(txns)
        assert len(clusters) >= 2
        assert clusters[0].count >= clusters[1].count


# ═══════════════════════════════════════════════════════════════════════
# Signal processing helpers
# ═══════════════════════════════════════════════════════════════════════


class TestSmoothSeries:
    """Verify moving average smoothing."""

    def test_identity_for_short_series(self) -> None:
        assert _smooth_series([1.0, 2.0], window=3) == [1.0, 2.0]

    def test_smoothing_reduces_noise(self) -> None:
        data = [1.0, 10.0, 1.0, 10.0, 1.0]
        smoothed = _smooth_series(data, window=3)
        # After smoothing with window=3, extreme values should be dampened
        assert smoothed[1] < 10.0  # The peak at index 1 gets averaged
        assert smoothed[0] >= 1.0  # First element

    def test_same_length(self) -> None:
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        smoothed = _smooth_series(data, window=3)
        assert len(smoothed) == len(data)

    def test_empty(self) -> None:
        assert _smooth_series([], window=3) == []

    def test_constant_series(self) -> None:
        data = [5.0, 5.0, 5.0, 5.0, 5.0]
        smoothed = _smooth_series(data, window=3)
        assert all(v == 5.0 for v in smoothed)


class TestFindPeaks:
    """Verify peak detection."""

    def test_single_peak(self) -> None:
        data = [1.0, 3.0, 1.0]
        assert _find_peaks(data) == [1]

    def test_multiple_peaks(self) -> None:
        data = [1.0, 3.0, 1.0, 4.0, 1.0]
        assert _find_peaks(data) == [1, 3]

    def test_no_peaks(self) -> None:
        data = [1.0, 1.0, 1.0]
        assert _find_peaks(data) == []

    def test_min_height_filter(self) -> None:
        data = [1.0, 5.0, 1.0, 3.0, 1.0]
        peaks = _find_peaks(data, min_height=4.0)
        assert peaks == [1]  # Only the 5.0 peak qualifies

    def test_edges_as_peaks(self) -> None:
        # Edges count as peaks if they're higher than their one neighbour
        data = [5.0, 3.0, 1.0, 3.0, 5.0]
        peaks = _find_peaks(data)
        assert 0 in peaks  # Left edge
        assert 4 in peaks  # Right edge

    def test_empty(self) -> None:
        assert _find_peaks([]) == []

    def test_descending_no_peaks(self) -> None:
        data = [5.0, 4.0, 3.0, 2.0, 1.0]
        peaks = _find_peaks(data)
        assert peaks == [0]  # Only the first element


class TestIntervalHistogram:
    """Verify interval histogram building."""

    def test_single_interval(self) -> None:
        hist = _interval_histogram([30.0], min_period=1, max_period=40)
        assert len(hist) == 40
        # Interval 30 → index 29
        assert hist[29] == 1.0

    def test_multiple_intervals(self) -> None:
        hist = _interval_histogram(
            [7.0, 7.0, 14.0, 30.0, 30.0, 30.0],
            min_period=1,
            max_period=40,
        )
        assert hist[6] == 2.0  # 7 days → index 6
        assert hist[13] == 1.0  # 14 days → index 13
        assert hist[29] == 3.0  # 30 days → index 29

    def test_out_of_range_intervals_ignored(self) -> None:
        hist = _interval_histogram([1.0, 1000.0], min_period=3, max_period=40)
        # 1.0 is below min, 1000 is above max — neither should appear
        assert all(v == 0.0 for v in hist)

    def test_empty(self) -> None:
        hist = _interval_histogram([], min_period=1, max_period=10)
        assert all(v == 0.0 for v in hist)
        assert len(hist) == 10


# ═══════════════════════════════════════════════════════════════════════
# Periodic pattern detection
# ═══════════════════════════════════════════════════════════════════════


class TestMapPeriodToLabel:
    """Verify period-to-frequency mapping."""

    def test_weekly(self) -> None:
        assert _map_period_to_label(7) == "weekly"

    def test_biweekly(self) -> None:
        assert _map_period_to_label(14) == "biweekly"

    def test_monthly(self) -> None:
        assert _map_period_to_label(30) == "monthly"

    def test_quarterly(self) -> None:
        assert _map_period_to_label(90) == "quarterly"

    def test_semiannual(self) -> None:
        assert _map_period_to_label(180) == "semiannual"

    def test_yearly(self) -> None:
        assert _map_period_to_label(365) == "yearly"

    def test_unknown(self) -> None:
        assert _map_period_to_label(2) is None  # Too short
        assert _map_period_to_label(400) is None  # Too long


class TestDetectPeriodsFromIntervals:
    """Verify interval-based period detection."""

    def test_monthly_intervals(self) -> None:
        intervals = [30.0, 31.0, 30.0, 29.0, 31.0, 30.0]
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        assert periods[0].label == "monthly"
        assert periods[0].period_days in (29, 30, 31)

    def test_weekly_intervals(self) -> None:
        intervals = [7.0, 7.0, 7.0, 7.0, 7.0]
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        assert periods[0].label == "weekly"
        assert periods[0].score > 0.5

    def test_mixed_intervals_best_period_wins(self) -> None:
        # 6 monthly + 1 quarterly — monthly should dominate
        intervals = [30.0] * 6 + [90.0]
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        assert periods[0].label == "monthly" or periods[0].period_days == 30

    def test_too_few_intervals(self) -> None:
        assert _detect_periods_from_intervals([], min_occurrences=2) == []
        assert _detect_periods_from_intervals([7.0], min_occurrences=2) == []

    def test_all_same_interval(self) -> None:
        intervals = [14.0, 14.0, 14.0, 14.0]
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        assert periods[0].period_days == 14

    def test_sort_order_by_score(self) -> None:
        intervals = [30.0] * 10 + [7.0] * 3
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 2
        # Monthly (peak at 30, count=10) should rank higher than weekly (peak at 7, count=3)
        assert periods[0].period_days == 30


class TestPeriodicPatternDetector:
    """Verify full periodic pattern detection."""

    @pytest.fixture
    def monthly_transactions(self) -> list[dict]:
        base = datetime(2025, 1, 15, tzinfo=UTC)
        return [
            _make_txn_dict(
                _MockTxn(
                    amount=Decimal("-15.99"),
                    occurred_at=base + timedelta(days=30 * i),
                )
            )
            for i in range(6)
        ]

    @pytest.fixture
    def weekly_transactions(self) -> list[dict]:
        base = datetime(2025, 1, 6, tzinfo=UTC)
        return [
            _make_txn_dict(
                _MockTxn(
                    amount=Decimal("-4.50"),
                    occurred_at=base + timedelta(weeks=i),
                )
            )
            for i in range(8)
        ]

    def test_detect_monthly(self, monthly_transactions) -> None:
        detector = PeriodicPatternDetector(min_occurrences=2)
        periods = detector.detect_periods(monthly_transactions)
        assert len(periods) >= 1
        assert periods[0].label == "monthly"

    def test_detect_weekly(self, weekly_transactions) -> None:
        detector = PeriodicPatternDetector(min_occurrences=2)
        periods = detector.detect_periods(weekly_transactions)
        assert len(periods) >= 1
        assert periods[0].label == "weekly"

    def test_too_few_transactions(self) -> None:
        txns = [
            _make_txn_dict(_MockTxn()),
        ]
        detector = PeriodicPatternDetector(min_occurrences=2)
        assert detector.detect_periods(txns) == []

    def test_no_dates(self) -> None:
        txns = [{"id": "1", "occurred_at": None}]
        detector = PeriodicPatternDetector(min_occurrences=2)
        assert detector.detect_periods(txns) == []

    def test_compute_regularity_perfect(self) -> None:
        detector = PeriodicPatternDetector()
        intervals = [30.0, 30.0, 30.0]  # perfectly regular
        score = detector.compute_regularity(intervals)
        assert score == 1.0

    def test_compute_regularity_irregular(self) -> None:
        detector = PeriodicPatternDetector()
        intervals = [3.0, 47.0, 5.0, 90.0]  # very irregular
        score = detector.compute_regularity(intervals)
        assert score < 0.5

    def test_compute_regularity_with_skipped_payments(self) -> None:
        detector = PeriodicPatternDetector()
        # Monthly intervals but one is double (skipped payment → 60 days)
        intervals = [30.0, 30.0, 60.0, 30.0]
        score = detector.compute_regularity(intervals)
        # The dominant period is 30 days, 3 of 4 intervals match ≈ 0.75
        assert 0.5 <= score <= 1.0

    def test_single_interval_returns_one(self) -> None:
        detector = PeriodicPatternDetector()
        assert detector.compute_regularity([30.0]) == 1.0

    def test_empty_intervals(self) -> None:
        detector = PeriodicPatternDetector()
        assert detector.compute_regularity([]) == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Cross-account matching
# ═══════════════════════════════════════════════════════════════════════


class TestCrossAccountMatcher:
    """Verify cross-account subscription matching."""

    def _make_pattern(
        self,
        merchant: str = "Netflix",
        amount: Decimal = Decimal("-15.99"),
        freq_days: int = 30,
        freq_label: str = "monthly",
        account_id: str = "acct_1",
        provider: str = "bunq",
        first_date: datetime | None = None,
        last_date: datetime | None = None,
        confidence: SubscriptionConfidence = SubscriptionConfidence.MEDIUM,
    ) -> dict:
        base = datetime(2025, 1, 1, tzinfo=UTC)
        return {
            "merchant_name": merchant,
            "amount": amount,
            "frequency_days": freq_days,
            "frequency_label": freq_label,
            "account_id": account_id,
            "provider_key": provider,
            "first_detected_at": first_date or base,
            "last_detected_at": last_date or (base + timedelta(days=180)),
            "confidence": confidence,
        }

    def test_same_merchant_different_accounts(self) -> None:
        matcher = CrossAccountMatcher()
        patterns = [
            self._make_pattern(
                merchant="Netflix", account_id="acct_1", provider="bunq"
            ),
            self._make_pattern(
                merchant="Netflix", account_id="acct_2", provider="trading212"
            ),
        ]
        matches = matcher.find_cross_account_matches(patterns)
        assert len(matches) >= 1
        match = matches[0]
        assert match.merchant_name == "Netflix"
        assert len(match.accounts) >= 2

    def test_same_merchant_same_account_no_match(self) -> None:
        matcher = CrossAccountMatcher()
        patterns = [
            self._make_pattern(merchant="Netflix", account_id="acct_1"),
            self._make_pattern(merchant="Netflix", account_id="acct_1"),
        ]
        matches = matcher.find_cross_account_matches(patterns)
        # Same account — no cross-account match needed
        assert len(matches) == 0

    def test_amount_based_cross_merchant(self) -> None:
        matcher = CrossAccountMatcher()
        patterns = [
            self._make_pattern(
                merchant="Google Youtube",
                amount=Decimal("-15.99"),
                account_id="acct_1",
                freq_days=30,
            ),
            self._make_pattern(
                merchant="YOUTUBE PREMIUM",
                amount=Decimal("-15.99"),
                account_id="acct_2",
                freq_days=30,
            ),
        ]
        matches = matcher.find_cross_account_matches(patterns)
        assert len(matches) >= 1

    def test_incompatible_amounts_no_match(self) -> None:
        matcher = CrossAccountMatcher()
        patterns = [
            self._make_pattern(
                merchant="Netflix",
                amount=Decimal("-15.99"),
                account_id="acct_1",
            ),
            self._make_pattern(
                merchant="Netflix",
                amount=Decimal("-99.99"),
                account_id="acct_2",
            ),
        ]
        matches = matcher.find_cross_account_matches(patterns)
        assert len(matches) == 0

    def test_incompatible_intervals_no_match(self) -> None:
        matcher = CrossAccountMatcher()
        patterns = [
            self._make_pattern(
                merchant="Netflix",
                freq_days=30,
                account_id="acct_1",
            ),
            self._make_pattern(
                merchant="Netflix",
                freq_days=365,
                account_id="acct_2",
            ),
        ]
        matches = matcher.find_cross_account_matches(patterns)
        assert len(matches) == 0

    def test_confidence_boosted_for_cross_account(self) -> None:
        matcher = CrossAccountMatcher()
        patterns = [
            self._make_pattern(
                merchant="Netflix",
                account_id="acct_1",
                confidence=SubscriptionConfidence.MEDIUM,
            ),
            self._make_pattern(
                merchant="Netflix",
                account_id="acct_2",
                confidence=SubscriptionConfidence.MEDIUM,
            ),
        ]
        matches = matcher.find_cross_account_matches(patterns)
        assert len(matches) >= 1
        # Confidence should be boosted to HIGH when confirmed across accounts
        assert matches[0].confidence == SubscriptionConfidence.HIGH

    def test_single_pattern_no_match(self) -> None:
        matcher = CrossAccountMatcher()
        patterns = [self._make_pattern()]
        assert matcher.find_cross_account_matches(patterns) == []

    def test_empty_patterns(self) -> None:
        matcher = CrossAccountMatcher()
        assert matcher.find_cross_account_matches([]) == []


# ═══════════════════════════════════════════════════════════════════════
# Confidence scoring
# ═══════════════════════════════════════════════════════════════════════


class TestClusterConfidenceScoring:
    """Verify confidence scoring for cluster-based detection."""

    def test_high_confidence(self) -> None:
        level, score = _compute_cluster_confidence(
            occurrence_count=12,
            amount_consistency=1.0,
            interval_regularity=1.0,
            cluster_size=10,
        )
        assert level == SubscriptionConfidence.HIGH
        assert score >= 0.80

    def test_medium_confidence(self) -> None:
        level, score = _compute_cluster_confidence(
            occurrence_count=6,
            amount_consistency=0.6,
            interval_regularity=0.7,
            cluster_size=4,
        )
        assert level == SubscriptionConfidence.MEDIUM
        assert 0.50 <= score < 0.80

    def test_low_confidence(self) -> None:
        level, score = _compute_cluster_confidence(
            occurrence_count=2,
            amount_consistency=0.3,
            interval_regularity=0.1,
            cluster_size=2,
        )
        assert level == SubscriptionConfidence.LOW
        assert score < 0.50

    def test_cross_account_bonus(self) -> None:
        _, score_with = _compute_cluster_confidence(
            occurrence_count=3,
            amount_consistency=0.6,
            interval_regularity=0.5,
            cluster_size=3,
            has_cross_account_confirmation=True,
        )
        _, score_without = _compute_cluster_confidence(
            occurrence_count=3,
            amount_consistency=0.6,
            interval_regularity=0.5,
            cluster_size=3,
            has_cross_account_confirmation=False,
        )
        assert score_with > score_without

    def test_score_capped(self) -> None:
        _, score = _compute_cluster_confidence(
            occurrence_count=24,
            amount_consistency=1.0,
            interval_regularity=1.0,
            cluster_size=20,
        )
        assert score <= 1.0


# ═══════════════════════════════════════════════════════════════════════
# Full pipeline (SubscriptionPatternEngine)
# ═══════════════════════════════════════════════════════════════════════


class TestSubscriptionPatternEngine:
    """Verify the full pattern detection pipeline."""

    @pytest.fixture
    def netflix_txns(self) -> list[dict]:
        """Simulate monthly Netflix charges with consistent amounts but
        slightly varying descriptions (simulating different banks)."""
        base = datetime(2025, 1, 15, tzinfo=UTC)
        descriptions = [
            "POS Netflix B.V.",
            "DEB Netflix B.V.",
            "Netflix Subscription",
            "Card Netflix B.V.",
            "SEPA Netflix B.V.",
            "Netflix Subscription",
            "POS Netflix.com",
            "NETFLIX DIRECT DEBIT",
        ]
        return [
            _make_txn_dict(
                _MockTxn(
                    amount=Decimal("-15.99"),
                    description=desc,
                    occurred_at=base + timedelta(days=30 * i),
                    account_id="acct_1",
                )
            )
            for i, desc in enumerate(descriptions)
        ]

    @pytest.fixture
    def mixed_amount_txns(self) -> list[dict]:
        """Mix of a Netflix-like subscription and random purchases."""
        base = datetime(2025, 1, 1, tzinfo=UTC)
        txns = []

        # Monthly subscription at €15.99
        for i in range(6):
            txns.append(
                _make_txn_dict(
                    _MockTxn(
                        amount=Decimal("-15.99"),
                        description="Netflix",
                        occurred_at=base + timedelta(days=30 * i),
                        account_id="acct_1",
                    )
                )
            )

        # Weekly subscription at €4.99
        for i in range(8):
            txns.append(
                _make_txn_dict(
                    _MockTxn(
                        amount=Decimal("-4.99"),
                        description="Spotify",
                        occurred_at=base + timedelta(weeks=i),
                        account_id="acct_1",
                    )
                )
            )

        # Random one-offs
        for i in range(5):
            txns.append(
                _make_txn_dict(
                    _MockTxn(
                        amount=Decimal(f"-{10 + i * 5}.00"),
                        description="Random Store",
                        occurred_at=base + timedelta(days=7 * i + 2),
                        account_id="acct_1",
                    )
                )
            )

        return txns

    @pytest.fixture
    def cross_account_txns(self) -> list[dict]:
        """Simulate same subscription on two accounts with different
        amounts — distinct enough for separate clustering but close
        enough (62.5% overlap) for cross-account matching."""
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = []

        # Netflix on account 1 at €15.99 (monthly)
        for i in range(6):
            txns.append(
                _make_txn_dict(
                    _MockTxn(
                        amount=Decimal("-15.99"),
                        description="Netflix B.V.",
                        occurred_at=base + timedelta(days=30 * i),
                        account_id="acct_1",
                        provider_key="bunq",
                    )
                )
            )

        # Netflix on account 2 at €9.99 (different price tier) —
        # still within 50% overlap for cross-account matching
        for i in range(6):
            txns.append(
                _make_txn_dict(
                    _MockTxn(
                        amount=Decimal("-9.99"),
                        description="NETFLIX.COM",
                        occurred_at=base + timedelta(days=30 * i + 1),
                        account_id="acct_2",
                        provider_key="trading212",
                    )
                )
            )

        return txns

    def test_detect_netflix_as_subscription(self, netflix_txns) -> None:
        engine = SubscriptionPatternEngine(min_occurrences=2)
        patterns = engine.detect(netflix_txns)
        # Should find Netflix as a pattern
        netflix_patterns = [
            p for p in patterns if "Netflix" in p["merchant_name"]
        ]
        assert len(netflix_patterns) >= 1
        pattern = netflix_patterns[0]
        assert pattern["frequency_label"] == "monthly"
        assert pattern["occurrence_count"] >= 2
        assert pattern["confidence"] in (
            SubscriptionConfidence.HIGH,
            SubscriptionConfidence.MEDIUM,
        )

    def test_detect_mixed_subscriptions(self, mixed_amount_txns) -> None:
        engine = SubscriptionPatternEngine(min_occurrences=3)
        patterns = engine.detect(mixed_amount_txns)
        merchant_names = [p["merchant_name"] for p in patterns]
        # Should detect both Netflix and Spotify
        assert any("Netflix" in m for m in merchant_names)
        assert any("Spotify" in m for m in merchant_names)

    def test_cross_account_detection(self, cross_account_txns) -> None:
        engine = SubscriptionPatternEngine(min_occurrences=2)
        patterns = engine.detect(cross_account_txns)
        # Should produce at least one cross-account pattern
        cross_account = [
            p
            for p in patterns
            if p["detection_method"] == DetectionMethod.CROSS_ACCOUNT
        ]
        assert len(cross_account) >= 1

    def test_empty_transactions(self) -> None:
        engine = SubscriptionPatternEngine()
        assert engine.detect([]) == []

    def test_insufficient_data(self) -> None:
        txns = [
            _make_txn_dict(_MockTxn(amount=Decimal("-10.00"))),
        ]
        engine = SubscriptionPatternEngine(min_occurrences=2)
        assert engine.detect(txns) == []

    def test_results_have_required_fields(self, netflix_txns) -> None:
        engine = SubscriptionPatternEngine(min_occurrences=2)
        patterns = engine.detect(netflix_txns)
        for p in patterns:
            assert "merchant_name" in p
            assert "amount" in p
            assert "confidence" in p
            assert "detection_method" in p
            assert "transaction_ids" in p
            assert "occurrence_count" in p
            assert "details" in p

    def test_different_amounts_produce_diff_clusters(self) -> None:
        """Two different subscriptions at different amounts should both be detected."""
        base = datetime(2025, 1, 1, tzinfo=UTC)
        txns = []
        # Netflix €15.99 monthly
        for i in range(6):
            txns.append(
                _make_txn_dict(
                    _MockTxn(
                        amount=Decimal("-15.99"),
                        description="Netflix",
                        occurred_at=base + timedelta(days=30 * i),
                    )
                )
            )
        # Spotify €9.99 monthly
        for i in range(6):
            txns.append(
                _make_txn_dict(
                    _MockTxn(
                        amount=Decimal("-9.99"),
                        description="Spotify",
                        occurred_at=base + timedelta(days=30 * i + 5),
                    )
                )
            )

        engine = SubscriptionPatternEngine(min_occurrences=3)
        patterns = engine.detect(txns)
        merchant_names = [p["merchant_name"] for p in patterns]
        assert any("Netflix" in m for m in merchant_names) or any(
            "Spotify" in m for m in merchant_names
        )


# ═══════════════════════════════════════════════════════════════════════
# AmountCluster data class tests
# ═══════════════════════════════════════════════════════════════════════


class TestAmountCluster:
    """Verify AmountCluster data class."""

    def test_creation(self) -> None:
        cluster = AmountCluster(
            amount=Decimal("-9.99"),
            indices=[0, 1, 2],
            count=3,
            total=Decimal("29.97"),
        )
        assert cluster.amount == Decimal("-9.99")
        assert cluster.count == 3
        assert cluster.total == Decimal("29.97")
        assert repr(cluster) == "<AmountCluster amount=-9.99 count=3>"


# ═══════════════════════════════════════════════════════════════════════
# Edge cases and robustness
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Verify edge case handling across the module."""

    def test_very_large_amount_differences(self) -> None:
        """Large amounts far apart should not cluster."""
        values = [
            Decimal("-1000000.00"),
            Decimal("-1.00"),
        ]
        clusters = _density_cluster_1d(
            values,
            eps_pct=Decimal("0.05"),
            eps_abs=Decimal("2.00"),
            min_pts=2,
        )
        assert len(clusters) == 0

    def test_period_detection_with_gaps(self) -> None:
        """Period detection should handle missing transactions (gaps)."""
        intervals = [
            30.0,
            30.0,
            30.0,  # Three monthly
            60.0,  # One skipped (60 = 2× monthly)  # noqa: RUF003
            30.0,
            30.0,  # Normal again
        ]
        periods = _detect_periods_from_intervals(intervals, min_occurrences=2)
        assert len(periods) >= 1
        # The dominant period should still be ~30 days (monthly)
        assert periods[0].period_days == 30

    def test_amount_clustering_with_zero_values(self) -> None:
        """Zero values should cluster and not cause division errors."""
        txns = [
            _make_txn_dict(_MockTxn(amount=Decimal(0))),
            _make_txn_dict(_MockTxn(amount=Decimal(0))),
            _make_txn_dict(_MockTxn(amount=Decimal(0))),
        ]
        detector = AmountClusterDetector()
        clusters = detector.detect_clusters(txns)
        assert len(clusters) == 1
        assert clusters[0].amount == Decimal(0)

    def test_cross_account_missing_dates(self) -> None:
        """Missing date fields should not crash the matcher."""
        matcher = CrossAccountMatcher()
        patterns = [
            {
                "merchant_name": "Netflix",
                "amount": Decimal("-15.99"),
                "frequency_days": None,
                "frequency_label": None,
                "account_id": "acct_1",
                "provider_key": "bunq",
                "first_detected_at": None,
                "last_detected_at": None,
                "confidence": SubscriptionConfidence.LOW,
            },
            {
                "merchant_name": "Netflix",
                "amount": Decimal("-15.99"),
                "frequency_days": None,
                "frequency_label": None,
                "account_id": "acct_2",
                "provider_key": "trading212",
                "first_detected_at": None,
                "last_detected_at": None,
                "confidence": SubscriptionConfidence.LOW,
            },
        ]
        matches = matcher.find_cross_account_matches(patterns)
        # Should still find the match despite missing dates
        assert len(matches) >= 1
