"""Tests for enhanced pattern detection: variable amounts and skipped payments.

Covers:
- _amounts_step_change_score: edge cases, cluster boundaries
- _detect_frequency_robust: skipped payments, outlier filtering
- PatternDetector.detect() with price-change patterns (step changes)
- PatternDetector.detect() with skipped payment intervals
- PatternDetector.detect() with both step changes + irregular intervals
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from finance_sync.models.enums import SubscriptionConfidence
from finance_sync.services.subscription_detector.detector import (
    _amounts_step_change_score,
    _detect_frequency_robust,
)
from finance_sync.services.subscription_detector.pattern_detector import (
    PatternDetector,
)

# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_txn(
    *,
    amount: Decimal = Decimal("-9.99"),
    description: str = "Netflix",
    occurred_at: datetime | None = None,
    account_id: str = "acct_1",
    provider_key: str = "bunq",
    transaction_type: str = "payment",
    currency_code: str = "EUR",
    txn_id: str | None = None,
) -> dict:
    return {
        "id": txn_id or str(uuid4()),
        "amount": amount,
        "currency_code": currency_code,
        "description": description,
        "occurred_at": occurred_at or datetime(2025, 1, 15, tzinfo=UTC),
        "account_id": account_id,
        "provider_key": provider_key,
        "transaction_type": transaction_type,
    }


# ═══════════════════════════════════════════════════════════════════════
# _amounts_step_change_score
# ═══════════════════════════════════════════════════════════════════════


class TestAmountStepChangeScore:
    """Cover _amounts_step_change_score boundaries."""

    def test_two_clusters_returns_0_5(self) -> None:
        """Two distinct amount levels — clear price change."""
        amounts = [
            Decimal("7.99"),
            Decimal("7.99"),
            Decimal("7.99"),
            Decimal("15.99"),
            Decimal("15.99"),
            Decimal("15.99"),
        ]
        score = _amounts_step_change_score(amounts)
        assert score == 0.5

    def test_three_clusters_returns_0_5(self) -> None:
        """Three distinct amount levels (e.g. multiple price increases)."""
        amounts = [
            Decimal("7.99"),
            Decimal("7.99"),
            Decimal("9.99"),
            Decimal("9.99"),
            Decimal("12.99"),
            Decimal("12.99"),
        ]
        score = _amounts_step_change_score(amounts)
        assert score == 0.5

    def test_single_cluster_returns_0_6(self) -> None:
        """Single tight cluster — amounts should have been caught earlier."""
        amounts = [Decimal("15.99"), Decimal("15.99"), Decimal("15.99")]
        score = _amounts_step_change_score(amounts)
        assert score == 0.6

    def test_noise_returns_0_0(self) -> None:
        """Widely scattered amounts — no pattern."""
        amounts = [Decimal("10.00"), Decimal("50.00"), Decimal("100.00")]
        score = _amounts_step_change_score(amounts)
        assert score == 0.0

    def test_four_plus_clusters_returns_0_0(self) -> None:
        """4+ clusters means too many distinct levels."""
        amounts = [
            Decimal("5.00"),
            Decimal("5.00"),
            Decimal("10.00"),
            Decimal("10.00"),
            Decimal("20.00"),
            Decimal("20.00"),
            Decimal("40.00"),
            Decimal("40.00"),
        ]
        score = _amounts_step_change_score(amounts)
        assert score == 0.0

    def test_less_than_3_amounts_returns_0_0(self) -> None:
        """Fewer than 3 amounts can't form a meaningful step pattern."""
        score = _amounts_step_change_score([Decimal("10.00"), Decimal("20.00")])
        assert score == 0.0

    def test_small_absolute_fluctuation_single_cluster(self) -> None:
        """Amounts within EUR 2 tolerance form a single cluster -> 0.6."""
        amounts = [Decimal("100.00"), Decimal("101.50"), Decimal("99.00")]
        score = _amounts_step_change_score(amounts)
        assert score == 0.6

    def test_gradual_price_increase_partial(self) -> None:
        """Gradual increase forming 2 distinct clusters is detected."""
        amounts = [
            Decimal("7.99"),
            Decimal("7.99"),
            Decimal("15.99"),
            Decimal("15.99"),
        ]
        score = _amounts_step_change_score(amounts)
        assert score == 0.5


# ═══════════════════════════════════════════════════════════════════════
# _detect_frequency_robust
# ═══════════════════════════════════════════════════════════════════════


class TestDetectFrequencyRobust:
    """Cover _detect_frequency_robust — skipped-payment tolerance."""

    def test_regular_intervals(self) -> None:
        """Normal monthly intervals pass through unchanged."""
        _days, label = _detect_frequency_robust([30.0, 30.0, 30.0])
        assert label == "monthly"

    def test_regular_weekly(self) -> None:
        """Weekly intervals."""
        _days, label = _detect_frequency_robust([7.0, 7.0, 8.0, 7.0])
        assert label == "weekly"

    def test_single_skipped_payment(self) -> None:
        """Monthly with one 60-day interval (skipped payment)."""
        _days, label = _detect_frequency_robust([30.0, 60.0, 30.0])
        assert label == "monthly"

    def test_two_skipped_payments_no_dominant_freq(self) -> None:
        """Two 60-day gaps — median skewed, no dominant label."""
        _, label = _detect_frequency_robust([30.0, 60.0, 60.0, 30.0])
        # With equal regular and skipped intervals, median is in
        # no-man's-land between monthly and quarterly -> no label
        assert label is None

    def test_empty_returns_none(self) -> None:
        """Empty input returns None."""
        assert _detect_frequency_robust([]) == (None, None)

    def test_single_interval_delegates(self) -> None:
        """Single interval delegates to standard _detect_frequency."""
        _days, label = _detect_frequency_robust([30.0])
        assert label == "monthly"

    def test_mixed_bands_with_outlier(self) -> None:
        """Outlier doesn't prevent detection of the dominant band."""
        # [90, 30, 90, 90] -> median=90, outlier=30 (< 0.5 x 90) -> filtered
        # cleaned = [90, 90, 90] -> quarterly
        label = _detect_frequency_robust([90.0, 30.0, 90.0, 90.0])[1]
        assert label == "quarterly"

    def test_all_outliers_truly_random(self) -> None:
        """Widely varying intervals with no dominant frequency."""
        label = _detect_frequency_robust([3.0, 47.0, 5.0, 90.0])[1]
        assert label is None


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — variable amounts (step changes)
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorVariableAmounts:
    """PatternDetector.detect() with price-change patterns."""

    def test_two_price_levels_detected(self) -> None:
        """Subscription with a price increase from EUR 9.99 to EUR 15.99."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-9.99"),
                description="Netflix Subscription",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(3)
        ] + [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix Subscription",
                occurred_at=base + timedelta(days=30 * (i + 3)),
            )
            for i in range(3)
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert "Netflix" in r.merchant_name
        assert r.frequency_label == "monthly"
        assert 0.10 < r.detection_score < 0.99
        assert r.occurrence_count == 6

    def test_three_price_levels_detected(self) -> None:
        """Subscription with two price increases over time."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = []
        for level in [Decimal("-7.99"), Decimal("-9.99"), Decimal("-12.99")]:
            for _i in range(2):
                idx = len(txns)
                txns.append(
                    _make_txn(
                        amount=level,
                        description="Streaming Service",
                        occurred_at=base + timedelta(days=30 * idx),
                    )
                )
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert r.frequency_label == "monthly"
        assert r.occurrence_count == 6

    def test_price_decrease_still_detected(self) -> None:
        """Subscription with a price decrease (downgrade)."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-19.99"),
                description="Premium Service",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(3)
        ] + [
            _make_txn(
                amount=Decimal("-9.99"),
                description="Premium Service",
                occurred_at=base + timedelta(days=30 * (i + 3)),
            )
            for i in range(3)
        ]
        results = detector.detect(txns)
        assert len(results) >= 1

    def test_gradual_price_change_stays_high_confidence(self) -> None:
        """Small gradual increases within 5% stay high confidence."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # EUR 10.00 -> EUR 10.30 -> EUR 10.60 -> EUR 10.90 (each step ~3%)
        txns = [
            _make_txn(
                amount=Decimal(f"-{10.00 + i * 0.30:.2f}"),
                description="Service",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(4)
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        # All amounts within 5% of mean, so confidence >= MEDIUM
        assert r.confidence in (
            SubscriptionConfidence.HIGH,
            SubscriptionConfidence.MEDIUM,
        )


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — irregular intervals (skipped payments)
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorIrregularIntervals:
    """PatternDetector.detect() with skipped payments."""

    def test_one_skipped_month(self) -> None:
        """Monthly subscription with one skipped payment (60-day gap)."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=60),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=120),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=150),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert r.frequency_label == "monthly"
        assert r.frequency_days == 30

    def test_one_extra_long_gap(self) -> None:
        """Quarterly subscription with one skipped quarter."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-29.99"),
                description="Magazine Subscription",
                occurred_at=base + timedelta(days=90 * i),
            )
            for i in range(3)
        ] + [
            _make_txn(
                amount=Decimal("-29.99"),
                description="Magazine Subscription",
                occurred_at=base + timedelta(days=90 * 3 + 180),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert r.frequency_label == "quarterly"

    def test_mixed_irregular_still_detects_if_dominant(self) -> None:
        """When the dominant pattern is clear despite some irregularity."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-4.99"),
                description="Weekly Service",
                occurred_at=base + timedelta(days=7 * i),
            )
            for i in range(5)
        ] + [
            _make_txn(
                amount=Decimal("-4.99"),
                description="Weekly Service",
                occurred_at=base + timedelta(days=7 * 5 + 14),
            ),
            _make_txn(
                amount=Decimal("-4.99"),
                description="Weekly Service",
                occurred_at=base + timedelta(days=7 * 6 + 14),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert r.frequency_label == "weekly"


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — combined variable amounts + irregular intervals
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorCombined:
    """PatternDetector with both price changes AND skipped payments."""

    def test_price_change_and_skipped_payment(self) -> None:
        """Realistic scenario: price increase AND one skipped month."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-9.99"),
                description="Streaming Subscription",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-9.99"),
                description="Streaming Subscription",
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-9.99"),
                description="Streaming Subscription",
                occurred_at=base + timedelta(days=60),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Streaming Subscription",
                occurred_at=base + timedelta(days=90),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Streaming Subscription",
                occurred_at=base + timedelta(days=150),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Streaming Subscription",
                occurred_at=base + timedelta(days=180),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert "Streaming" in r.merchant_name
        assert r.frequency_label == "monthly"
        assert r.occurrence_count == 6
        assert r.detection_score > 0.0

    def test_price_change_early_stop_late_resume(self) -> None:
        """Price change and 90-day gap then resume."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-7.99"),
                description="Premium",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-7.99"),
                description="Premium",
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-12.99"),
                description="Premium",
                occurred_at=base + timedelta(days=120),
            ),
            _make_txn(
                amount=Decimal("-12.99"),
                description="Premium",
                occurred_at=base + timedelta(days=150),
            ),
            _make_txn(
                amount=Decimal("-12.99"),
                description="Premium",
                occurred_at=base + timedelta(days=180),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert r.frequency_label == "monthly"


# ═══════════════════════════════════════════════════════════════════════
# _amounts_are_consistent — step-change fallback
# ═══════════════════════════════════════════════════════════════════════


class TestAmountsAreConsistentStepChange:
    """Verify _amounts_are_consistent falls through to step-change detection."""

    def test_price_increase_not_zero(self) -> None:
        """Price increase from EUR 7.99 to EUR 15.99 yields moderate score."""
        from finance_sync.services.subscription_detector.detector import (
            _amounts_are_consistent,
        )

        amounts = [
            Decimal("-7.99"),
            Decimal("-7.99"),
            Decimal("-7.99"),
            Decimal("-15.99"),
            Decimal("-15.99"),
            Decimal("-15.99"),
        ]
        score = _amounts_are_consistent(amounts)
        assert score == 0.5

    def test_moderate_increase_above_5_percent(self) -> None:
        """Increases above 5% but within 15% return 0.6."""
        from finance_sync.services.subscription_detector.detector import (
            _amounts_are_consistent,
        )

        amounts = [Decimal("-100.00"), Decimal("-115.00"), Decimal("-108.00")]
        score = _amounts_are_consistent(amounts)
        assert score == 0.6

    def test_truly_random_amounts_still_zero(self) -> None:
        """Completely random amounts still return 0.0."""
        from finance_sync.services.subscription_detector.detector import (
            _amounts_are_consistent,
        )

        amounts = [Decimal("-5.00"), Decimal("-50.00"), Decimal("-500.00")]
        score = _amounts_are_consistent(amounts)
        assert score == 0.0


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — ensure existing behavior is preserved
# ═══════════════════════════════════════════════════════════════════════


class TestExistingBehaviorPreserved:
    """Key pre-existing behaviours must still work."""

    def test_same_amount_monthly_high_confidence(self) -> None:
        """Identical amounts + regular intervals -> HIGH confidence."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix Subscription",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(6)
        ]
        results = detector.detect(txns)
        assert len(results) == 1
        r = results[0]
        assert r.confidence == SubscriptionConfidence.HIGH
        assert r.frequency_label == "monthly"

    def test_inconsistent_amounts_truly_noise(self) -> None:
        """Widely varying amounts for same merchant still rejected."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-10.00"),
                description="Some Store",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-50.00"),
                description="Some Store",
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-100.00"),
                description="Some Store",
                occurred_at=base + timedelta(days=60),
            ),
        ]
        results = detector.detect(txns)
        assert results == []

    def test_multiple_merchants_still_separate(self) -> None:
        """Different merchants still produce separate results."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = []
        for i in range(6):
            txns.append(
                _make_txn(
                    amount=Decimal("-15.99"),
                    description="Netflix",
                    occurred_at=base + timedelta(days=30 * i),
                )
            )
            txns.append(
                _make_txn(
                    amount=Decimal("-9.99"),
                    description="Spotify",
                    occurred_at=base + timedelta(days=30 * i),
                )
            )
        results = detector.detect(txns)
        assert len(results) == 2
        merchants = {r.merchant_name for r in results}
        assert merchants == {"Netflix", "Spotify"}

    def test_insufficient_occurrences(self) -> None:
        """Fewer than min_occurrences still yields empty."""
        detector = PatternDetector(min_occurrences=3)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(2)
        ]
        assert detector.detect(txns) == []
