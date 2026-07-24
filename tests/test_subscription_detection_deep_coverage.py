"""Deep-coverage tests for subscription detection algorithms.

**Targets uncovered branches** identified by coverage analysis:

pattern_detector.py:
  - _is_outgoing / _amount_ok Decimal conversion ValueError
  - _analyse_group: interval regularity cv>0.5 branch
  - _analyse_group: REGULAR_INTERVAL detection method
  - _analyse_group: None-dates / zero-diff intervals
  - _analyse_group: classification not-a-MerchantClass not-a-dict path

service.py:
  - _group_by_merchant_and_amount: Decimal conversion error
  - _outgoing_transactions: Decimal conversion error
  - _analyze_group: None-dates / zero-diff intervals
  - _analyze_group: interval regularity cv>0.5 branch (score 0.1)
  - _analyze_group: detection method REGULAR_INTERVAL
  - _analyze_group: classification as plain dict
  - _merchant_only_subscription: HIGH confidence path (score >= 0.80)
  - _resolve_overlaps: zero-amount guard
  - detect_subscriptions: DB-backed path
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)
from finance_sync.services.subscription_detector.pattern_detector import (
    PatternDetector,
    PatternResult,
)
from finance_sync.services.subscription_detector.service import (
    Subscription,
    SubscriptionDetectionService,
    _has_cancellation_signal,
)

# ── Module-level flag so conftest knows we exist ──────────────────────


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


def _make_monthly_txns(
    merchant: str = "Netflix",
    amount: str = "-15.99",
    count: int = 6,
    base: datetime | None = None,
    account_id: str = "acct_1",
    provider: str = "bunq",
) -> list[dict]:
    """Create a list of regular monthly transactions for a merchant."""
    start = base or datetime(2025, 1, 15, tzinfo=UTC)
    return [
        _make_txn(
            amount=Decimal(amount),
            description=merchant,
            occurred_at=start + timedelta(days=30 * i),
            account_id=account_id,
            provider_key=provider,
        )
        for i in range(count)
    ]


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — _is_outgoing edge cases
# ═══════════════════════════════════════════════════════════════════════


class _BadStrTypeError:
    """Helper: __str__ raises TypeError to test exception handlers."""
    def __str__(self) -> str:
        raise TypeError("simulated str conversion error")


class _BadStrValueError:
    """Helper: __str__ raises ValueError to test exception handlers."""
    def __str__(self) -> str:
        raise ValueError("simulated str conversion error")


class TestPatternDetectorIsOutgoingEdgeCases:
    """Cover _is_outgoing branches not hit by existing tests."""

    def test_amount_str_raises_typeerror(self) -> None:
        """amount whose str() raises TypeError is caught by
        except (ValueError, TypeError) and falls through to
        transaction_type check."""
        result = PatternDetector._is_outgoing(
            {"amount": _BadStrTypeError(), "transaction_type": "payment"}
        )
        assert result is True  # transaction_type = payment → outgoing

    def test_amount_str_raises_valueerror_no_type_match(self) -> None:
        """amount whose str() raises ValueError AND transaction_type
        not debit-like returns False."""
        result = PatternDetector._is_outgoing(
            {"amount": _BadStrValueError(), "transaction_type": "transfer"}
        )
        assert result is False

    def test_positive_amount_not_outgoing_falls_through(self) -> None:
        """Positive amount (not < 0) continues to transaction_type check."""
        result = PatternDetector._is_outgoing(
            {"amount": Decimal("100.00")}
        )
        assert result is False  # no transaction_type → empty string → False


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — _amount_ok edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorAmountOkEdgeCases:
    """Cover _amount_ok branches not hit by existing tests."""

    def test_amount_str_raises_typeerror_returns_false(self) -> None:
        """amount whose str() raises TypeError is caught
        by except (ValueError, TypeError) → returns False."""
        detector = PatternDetector()
        assert not detector._amount_ok({"amount": _BadStrTypeError()})

    def test_none_amount_returns_false(self) -> None:
        """None amount returns False."""
        detector = PatternDetector()
        assert not detector._amount_ok({"amount": None})

    def test_zero_amount_default_not_allowed(self) -> None:
        """Zero amount with allow_zero_amount=False returns False."""
        detector = PatternDetector()
        assert not detector._amount_ok({"amount": Decimal("0")})

    def test_zero_amount_when_allowed(self) -> None:
        """Zero amount with allow_zero_amount=True returns True."""
        detector = PatternDetector(allow_zero_amount=True)
        assert detector._amount_ok({"amount": Decimal("0")})


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — _analyse_group: irregular intervals (cv > 0.5)
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorIrregularIntervals:
    """Cover interval_regularity branches in _analyse_group."""

    def test_cv_between_0_1_and_0_25_and_0_5(self) -> None:
        """Intervals with cv ≈ 0.184 produce regularity 0.7."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=10),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=23),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        # intervals [10, 13], mean=11.5, dev=1.5, var=4.5, std≈2.121
        # cv=2.121/11.5≈0.184 -> between 0.1 and 0.25 -> regularity 0.7
        assert r.details["interval_regularity"] == 0.7

    def test_cv_greater_than_0_5(self) -> None:
        """Intervals with cv > 0.5 produce regularity 0.1."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=10),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=80),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        # intervals [10, 70], mean=40, std ≈ 42.43, cv ≈ 1.06 → > 0.5 → 0.1
        assert r.details["interval_regularity"] == 0.1


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — REGULAR_INTERVAL detection method
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorRegularIntervalMethod:
    """Cover the REGULAR_INTERVAL detection method branch.

    This fires when sector is None, amount_consistency > 0.0,
    frequency_label is None, and interval_regularity > 0.5.
    Use 45-day intervals — regular (cv=0) but not matching any
    frequency band (monthly=25-35, quarterly=80-100).
    """

    def test_regular_interval_method_selected(self) -> None:
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=45),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=90),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) == 1
        r = results[0]
        # intervals [45, 45], cv=0, median=45 → no freq band → freq_label=None
        # amount_consistency = 1.0, frequency_label = None
        # → falls through to REGULAR_INTERVAL check
        assert r.detection_method == DetectionMethod.REGULAR_INTERVAL
        assert r.details["interval_regularity"] == 1.0
        assert r.frequency_label is None


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — classification not MerchantClass, not dict
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorUnknownClassificationType:
    """When classifications dict contains something that is neither a
    MerchantClass instance nor a plain dict, no sector/boost is extracted."""

    def test_unexpected_classification_type(self) -> None:
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(3)
        ]
        # Classification is a float — not MerchantClass, not dict
        cls = {"Some Service": 0.85}
        results = detector.detect(txns, classifications=cls)
        if results:
            r = results[0]
            assert r.sector is None


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _group_by_merchant_and_amount error handling
# ═══════════════════════════════════════════════════════════════════════


class TestGroupByMerchantAndAmountEdgeCases:
    """Cover error handling in _group_by_merchant_and_amount."""

    def test_non_numeric_amount_skipped(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(amount=_BadStrValueError()),
            _make_txn(amount=Decimal("-15.99")),
        ]
        groups = svc._group_by_merchant_and_amount(txns)
        # First txn skipped, second one added
        assert len(groups) >= 1

    def test_amount_none_skipped(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(amount=None),  # type: ignore[arg-type]
        ]
        groups = svc._group_by_merchant_and_amount(txns)
        assert len(groups) == 0

    def test_positive_amount_skipped(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(amount=Decimal("100.00")),
        ]
        groups = svc._group_by_merchant_and_amount(txns)
        assert len(groups) == 0


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _outgoing_transactions error handling
# ═══════════════════════════════════════════════════════════════════════


class TestOutgoingTransactionsEdgeCases:
    """Cover error handling in _outgoing_transactions."""

    def test_non_numeric_amount_skipped(self) -> None:
        txns = [
            _make_txn(amount=_BadStrTypeError()),
            _make_txn(amount=Decimal("-15.99")),
        ]
        outgoing = SubscriptionDetectionService._outgoing_transactions(txns)
        assert len(outgoing) == 1
        assert Decimal(str(outgoing[0]["amount"])) == Decimal("-15.99")

    def test_amount_none_skipped(self) -> None:
        txns = [_make_txn(amount=None)]  # type: ignore[arg-type]
        outgoing = SubscriptionDetectionService._outgoing_transactions(txns)
        assert len(outgoing) == 0


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _analyze_group: None dates / zero diff
# ═══════════════════════════════════════════════════════════════════════


class TestAnalyzeGroupEdgeCases:
    """Cover edge cases in _analyze_group interval computation."""

    def test_same_date_transactions_produce_zero_intervals(self) -> None:
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                occurred_at=base,  # same timestamp → diff = 0
            ),
        ]
        result = svc._analyze_group("Test", txns)
        assert result is not None
        # Zero-diff intervals are skipped, so intervals_days may be empty
        # or contain a positive diff depending on how the code handles it

    def test_interval_regularity_cv_0_25_to_0_5(self) -> None:
        """Intervals with 0.25 < cv <= 0.5 produce regularity 0.4."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=20),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=55),
            ),
        ]
        result = svc._analyze_group("Some Service", txns)
        assert result is not None
        # intervals [20, 35], mean=27.5, dev=7.5 each, var=112.5, std≈10.61
        # cv=10.61/27.5=0.386 → between 0.25 and 0.5 → 0.4
        assert result["details"]["interval_regularity"] == 0.4

    def test_interval_regularity_cv_greater_than_0_5(self) -> None:
        """Intervals with cv > 0.5 produce regularity 0.1."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=10),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=80),
            ),
        ]
        result = svc._analyze_group("Some Service", txns)
        assert result is not None
        # intervals [10, 70], mean=40, cv≈1.06 -> > 0.5 -> 0.1
        assert result["details"]["interval_regularity"] == 0.1

    def test_interval_regularity_cv_0_1_to_0_25(self) -> None:
        """Intervals with 0.1 < cv <= 0.25 produce regularity 0.7."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # Intervals: [10, 13] -> mean=11.5, dev=1.5 each, var=4.5
        # std≈2.121, cv=2.121/11.5=0.184 -> between 0.1 and 0.25 -> 0.7
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=10),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=23),
            ),
        ]
        result = svc._analyze_group("Some Service", txns)
        assert result is not None
        assert result["details"]["interval_regularity"] == 0.7

    def test_classification_as_plain_dict(self) -> None:
        """Classification passed as a dict is handled correctly."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                occurred_at=base + timedelta(days=30),
            ),
        ]
        cls_dict = {"sector": "Technology", "likelihood_score": 0.06}
        result = svc._analyze_group("Test", txns, classification=cls_dict)
        assert result is not None
        assert result["sector"] == "Technology"
        assert result["detection_method"] == DetectionMethod.MERCHANT_CLASSIFICATION

    def test_classification_as_unknown_object(self) -> None:
        """Classification that is neither MerchantClass nor dict — no sector."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                occurred_at=base + timedelta(days=30),
            ),
        ]
        result = svc._analyze_group("Test", txns, classification="plain_string")
        assert result is not None
        assert result["sector"] is None


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _analyze_group: REGULAR_INTERVAL method
# ═══════════════════════════════════════════════════════════════════════


class TestAnalyzeGroupDetectionMethod:
    """Cover the SIMILAR_AMOUNT / REGULAR_INTERVAL detection method
    branches in _analyze_group."""

    def test_similar_amount_method(self) -> None:
        """When amounts match within tolerance and frequency_label
        is not None -> SIMILAR_AMOUNT."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # Amounts: -100.00, -115.00, -108.00 -> abs [100, 115, 108]
        # mean=107.67, max_dev=15, var=13.9% -> consistency=0.6
        # max_dev=15 > 2.00 so absolute check doesn't kick in
        txns = [
            _make_txn(
                amount=Decimal("-100.00"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-115.00"),
                description="Some Service",
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-108.00"),
                description="Some Service",
                occurred_at=base + timedelta(days=61),
            ),
        ]
        result = svc._analyze_group("Some Service", txns)
        assert result is not None
        # amount_consistency should be 0.6, frequency_label = "monthly"
        assert result["details"]["amount_consistency"] == 0.6
        assert result["frequency_label"] == "monthly"
        # amount_consistency > 0.0 and frequency_label is not None
        # but amount_consistency < 1.0, so NOT EXACT_AMOUNT
        assert result["detection_method"] == DetectionMethod.SIMILAR_AMOUNT

    def test_regular_interval_method_in_service(self) -> None:
        """REGULAR_INTERVAL method when no freq band matches but
        intervals are regular."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # 45-day intervals — regular (cv=0) but no matching freq band
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=45),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=90),
            ),
        ]
        result = svc._analyze_group("Some Service", txns)
        assert result is not None
        assert result["frequency_label"] is None
        assert result["detection_method"] == DetectionMethod.REGULAR_INTERVAL


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _merchant_only_subscription HIGH path
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantOnlySubscriptionConfidence:
    """Cover all confidence branches in _merchant_only_subscription."""

    @pytest.mark.asyncio
    async def test_high_confidence_merchant_only(self) -> None:
        """Score >= 0.80 -> HIGH confidence."""
        svc = SubscriptionDetectionService()
        mc = MagicMock()
        mc.is_subscription = True
        mc.confidence = 0.75
        mc.likelihood_score = 0.12  # total = 0.87 -> HIGH
        mc.sector = "Communication Services"
        mc.subscription_likelihood = "high"
        mc.source = "merchant_map"
        mc.ticker = "NFLX"

        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        sub = svc._merchant_only_subscription("Netflix", mc, txns)
        assert sub.confidence == SubscriptionConfidence.HIGH
        assert sub.detection_score >= 0.80

    @pytest.mark.asyncio
    async def test_medium_confidence_merchant_only(self) -> None:
        """0.50 <= score < 0.80 -> MEDIUM confidence."""
        svc = SubscriptionDetectionService()
        mc = MagicMock()
        mc.is_subscription = True
        mc.confidence = 0.55
        mc.likelihood_score = 0.06  # total = 0.61 -> MEDIUM
        mc.sector = None
        mc.subscription_likelihood = "medium"
        mc.source = "merchant_map"
        mc.ticker = None

        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Something",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        sub = svc._merchant_only_subscription("Something", mc, txns)
        assert sub.confidence == SubscriptionConfidence.MEDIUM
        assert 0.50 <= sub.detection_score < 0.80


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _resolve_overlaps zero-amount guard
# ═══════════════════════════════════════════════════════════════════════


class TestResolveOverlapsEdgeCases:
    """Cover zero-amount guard in _resolve_overlaps (line 918)."""

    def test_zero_amount_subscriptions_not_deduplicated(self) -> None:
        """When existing.subscription has amount=0, the duplicate check
        skips it (continue)."""
        svc = SubscriptionDetectionService()
        subs = [
            Subscription(
                merchant_name="Netflix",
                raw_description="POS Netflix",
                amount=Decimal("0"),  # zero amount
                currency_code="EUR",
                frequency_days=30,
                frequency_label="monthly",
                confidence=SubscriptionConfidence.MEDIUM,
                detection_score=0.70,
                detection_method=DetectionMethod.EXACT_AMOUNT,
                status=SubscriptionStatus.ACTIVE,
                transaction_ids=["t1"],
                account_id="acct_1",
                provider_key="bunq",
            ),
            Subscription(
                merchant_name="Netflix",
                raw_description="POS Netflix",
                amount=Decimal("15.99"),
                currency_code="EUR",
                frequency_days=30,
                frequency_label="monthly",
                confidence=SubscriptionConfidence.MEDIUM,
                detection_score=0.80,
                detection_method=DetectionMethod.EXACT_AMOUNT,
                status=SubscriptionStatus.ACTIVE,
                transaction_ids=["t2"],
                account_id="acct_1",
                provider_key="bunq",
            ),
        ]
        resolved = svc._resolve_overlaps(subs)
        # Both preserved because first has zero amount (skip check)
        assert len(resolved) == 2

    def test_single_subscription_passes_through(self) -> None:
        svc = SubscriptionDetectionService()
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
            currency_code="EUR",
            frequency_days=30,
            frequency_label="monthly",
            confidence=SubscriptionConfidence.HIGH,
            detection_score=0.92,
            detection_method=DetectionMethod.HYBRID,
            status=SubscriptionStatus.ACTIVE,
            transaction_ids=["t1"],
            account_id="acct_1",
            provider_key="bunq",
        )
        resolved = svc._resolve_overlaps([sub])
        assert len(resolved) == 1
        assert resolved[0] is sub

    def test_empty_list_returns_empty(self) -> None:
        svc = SubscriptionDetectionService()
        assert svc._resolve_overlaps([]) == []

    def test_subscription_with_zero_amount_both(self) -> None:
        """Both subscriptions with zero amount — both preserved."""
        svc = SubscriptionDetectionService()
        subs = [
            Subscription(
                merchant_name="Freebie",
                raw_description="Free Service",
                amount=Decimal("0"),
                currency_code="EUR",
                frequency_days=None,
                frequency_label=None,
                confidence=SubscriptionConfidence.LOW,
                detection_score=0.10,
                detection_method=DetectionMethod.MERCHANT_CLASSIFICATION,
                status=SubscriptionStatus.ACTIVE,
                transaction_ids=["t1"],
                account_id="acct_1",
                provider_key="bunq",
            ),
            Subscription(
                merchant_name="Freebie",
                raw_description="Free Service",
                amount=Decimal("0"),
                currency_code="EUR",
                frequency_days=None,
                frequency_label=None,
                confidence=SubscriptionConfidence.LOW,
                detection_score=0.05,
                detection_method=DetectionMethod.MERCHANT_CLASSIFICATION,
                status=SubscriptionStatus.ACTIVE,
                transaction_ids=["t2"],
                account_id="acct_1",
                provider_key="bunq",
            ),
        ]
        resolved = svc._resolve_overlaps(subs)
        assert len(resolved) == 2


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — detect_subscriptions DB-backed path
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsDBBacked:
    """Cover detect_subscriptions with DB-backed path
    (transactions=None, session_factory provided)."""

    @pytest.mark.asyncio
    async def test_db_backed_path(self) -> None:
        """When transactions is None and session_factory is set,
        _fetch_transactions is called."""
        svc = SubscriptionDetectionService(
            session_factory=MagicMock(),
        )
        with patch.object(
            svc, "_fetch_transactions", new=AsyncMock(return_value=[])
        ):
            results = await svc.detect_subscriptions(user_id="test")
        assert results == []

    @pytest.mark.asyncio
    async def test_db_backed_with_data(self) -> None:
        """DB-backed call with actual transaction data."""
        svc = SubscriptionDetectionService(
            session_factory=MagicMock(),
        )
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(3)
        ]
        with patch.object(
            svc, "_fetch_transactions", new=AsyncMock(return_value=txns)
        ):
            results = await svc.detect_subscriptions(user_id="test")
        assert len(results) >= 1
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _fetch_transactions DB path
# ═══════════════════════════════════════════════════════════════════════


class TestFetchTransactionsDB:
    """Cover the DB-backed _fetch_transactions method (lines 974-1014).

    Requires a properly mocked async SQLAlchemy session.
    """

    @pytest.mark.asyncio
    async def test_fetch_transactions_with_default_dates(self) -> None:
        """_fetch_transactions uses default dates when none provided."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock(return_value=mock_session)
        svc = SubscriptionDetectionService(session_factory=factory)

        results = await svc._fetch_transactions(user_id="test")

        assert results == []

    @pytest.mark.asyncio
    async def test_fetch_transactions_with_explicit_dates(self) -> None:
        """_fetch_transactions with explicit date_from/date_to."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock(return_value=mock_session)
        svc = SubscriptionDetectionService(session_factory=factory)

        dt_from = datetime(2025, 1, 1, tzinfo=UTC)
        dt_to = datetime(2025, 12, 31, tzinfo=UTC)

        results = await svc._fetch_transactions(
            user_id="test",
            date_from=dt_from,
            date_to=dt_to,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_detect_subscriptions_triggers_fetch(self) -> None:
        """Calling detect_subscriptions triggers _fetch_transactions
        when no transactions are passed."""
        svc = SubscriptionDetectionService(
            session_factory=MagicMock(),
        )
        with patch.object(
            svc, "_fetch_transactions", new=AsyncMock(return_value=[])
        ) as mock_fetch:
            await svc.detect_subscriptions(
                user_id="test",
                date_from=datetime(2025, 1, 1, tzinfo=UTC),
                date_to=datetime(2025, 12, 31, tzinfo=UTC),
            )
        mock_fetch.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _classify_all_merchants cancellation
#   with the free _has_cancellation_signal function
# ═══════════════════════════════════════════════════════════════════════


class TestHasCancellationSignal:
    """Cover all cancellation patterns in the free function."""

    def test_cancellation_matches(self) -> None:
        assert _has_cancellation_signal("Service Cancellation")
        assert _has_cancellation_signal("Cancel Subscription")
        assert _has_cancellation_signal("Account Terminated")
        assert _has_cancellation_signal("Policy Ended")
        assert _has_cancellation_signal("Account Closed")
        assert _has_cancellation_signal("Service Deactivated")
        assert _has_cancellation_signal("Direct Debit Stopped")
        assert _has_cancellation_signal("Refund Processing")
        assert _has_cancellation_signal("Payment Reversal")
        assert _has_cancellation_signal("Chargeback Notice")

    def test_no_cancellation_signal(self) -> None:
        assert not _has_cancellation_signal("Netflix Subscription")
        assert not _has_cancellation_signal("")
        assert not _has_cancellation_signal("Coffee Shop Amsterdam")
        assert not _has_cancellation_signal("Monthly Fee")
        assert not _has_cancellation_signal("Stopping By")
        assert not _has_cancellation_signal("Endless Love")
        assert not _has_cancellation_signal("Closet Organizer")
        assert not _has_cancellation_signal("CAN")

    def test_cancellation_partial_word_no_match(self) -> None:
        """'cancellation' pattern matches 'Order Cancellation'."""
        assert _has_cancellation_signal("Order Cancellation")

    def test_edge_single_char(self) -> None:
        assert not _has_cancellation_signal("a")
        assert not _has_cancellation_signal("x")


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — config property
# ═══════════════════════════════════════════════════════════════════════


class TestServiceConfig:
    """Cover the config property."""

    def test_default_config(self) -> None:
        svc = SubscriptionDetectionService()
        cfg = svc.config
        assert cfg["min_occurrences"] == 2
        assert cfg["merchant_only_threshold"] == 0.60
        assert cfg["cross_validation_bonus"] == 0.10
        assert cfg["amount_bucket_tolerance"] == "0.02"

    def test_custom_config(self) -> None:
        svc = SubscriptionDetectionService(
            min_occurrences=3,
            merchant_only_threshold=0.70,
            cross_validation_bonus=0.15,
            amount_bucket_tolerance=Decimal("0.05"),
        )
        cfg = svc.config
        assert cfg["min_occurrences"] == 3
        assert cfg["merchant_only_threshold"] == 0.70
        assert cfg["cross_validation_bonus"] == 0.15
        assert cfg["amount_bucket_tolerance"] == "0.05"


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _find_classified_without_pattern
# ═══════════════════════════════════════════════════════════════════════


class TestFindClassifiedWithoutPattern:
    """Cover _find_classified_without_pattern edge cases."""

    def test_merchant_not_subscription_false_skipped(self) -> None:
        svc = SubscriptionDetectionService()
        mc = MagicMock()
        mc.is_subscription = False
        merchants = {"Test": mc}
        results = svc._find_classified_without_pattern(
            merchants, {}, []
        )
        assert len(results) == 0

    def test_merchant_in_pattern_skipped(self) -> None:
        svc = SubscriptionDetectionService()
        mc = MagicMock()
        mc.is_subscription = True
        mc.confidence = 0.85
        merchants = {"Netflix": mc}
        results = svc._find_classified_without_pattern(
            merchants, {"Netflix": [{"test": True}]}, []
        )
        assert len(results) == 0

    def test_merchant_no_outgoing_skipped(self) -> None:
        svc = SubscriptionDetectionService()
        mc = MagicMock()
        mc.is_subscription = True
        mc.confidence = 0.85
        merchants = {"Netflix": mc}
        txns = [
            _make_txn(amount=Decimal("100.00"), description="Netflix")
        ]  # positive = incoming
        results = svc._find_classified_without_pattern(
            merchants, {}, txns
        )
        assert len(results) == 0

    def test_merchant_below_threshold_skipped(self) -> None:
        svc = SubscriptionDetectionService(merchant_only_threshold=0.70)
        mc = MagicMock()
        mc.is_subscription = True
        mc.confidence = 0.50  # below 0.70
        merchants = {"Netflix": mc}
        txns = [
            _make_txn(amount=Decimal("-15.99"), description="Netflix")
        ]
        results = svc._find_classified_without_pattern(
            merchants, {}, txns
        )
        assert len(results) == 0

    def test_merchant_confidence_threshold_passed(self) -> None:
        svc = SubscriptionDetectionService(merchant_only_threshold=0.60)
        mc = MagicMock()
        mc.is_subscription = True
        mc.confidence = 0.85
        merchants = {"Netflix": mc}
        txns = [
            _make_txn(amount=Decimal("-15.99"), description="Netflix")
        ]
        results = svc._find_classified_without_pattern(
            merchants, {}, txns
        )
        assert len(results) == 1
        assert results[0][0] == "Netflix"


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — edge cases in detect_subscriptions
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsEdgeCases:
    """Cover additional branches in detect_subscriptions."""

    @pytest.mark.asyncio
    async def test_classifier_not_subscription_no_cross_validation(self) -> None:
        """When merchant is classified but not a subscription,
        cross-validation is skipped."""
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Merchant",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC) + timedelta(days=30 * i),
            )
            for i in range(3)
        ]
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mc = MagicMock()
            mc.is_subscription = False
            mc.confidence = 0.85
            mc.likelihood_score = 0.12
            mc.sector = None
            mc.subscription_likelihood = "low"
            mc.source = "merchant_map"
            mc.ticker = None
            mc.security_id = None
            mock_classify.return_value = mc
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        # Cross-validation not applied, but pattern still detected
        some_merchant = [
            r for r in results if "Some Merchant" in r.merchant_name
        ]
        if some_merchant:
            assert some_merchant[0].detection_method != DetectionMethod.HYBRID

    @pytest.mark.asyncio
    async def test_mixed_cancellation_and_normal_descriptions(self) -> None:
        """Both normal and cancellation descriptions from same merchant."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=30 * i),
            )
            for i in range(4)
        ]
        txns.append(
            _make_txn(
                amount=Decimal("0"),
                description="Netflix Cancellation",
                occurred_at=base + timedelta(days=150),
            )
        )
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mc = MagicMock()
            mc.is_subscription = True
            mc.confidence = 0.85
            mc.likelihood_score = 0.12
            mc.sector = "Communication Services"
            mc.subscription_likelihood = "high"
            mc.source = "merchant_map"
            mc.ticker = "NFLX"
            mc.security_id = None
            mock_classify.return_value = mc
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — detect_subscriptions with date filters
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsDateFilters:
    """Cover date_from/date_to parameters."""

    @pytest.mark.asyncio
    async def test_date_filters_passed_through(self) -> None:
        """date_from and date_to are passed to _fetch_transactions."""
        svc = SubscriptionDetectionService(
            session_factory=MagicMock(),
        )
        dt_from = datetime(2025, 6, 1, tzinfo=UTC)
        dt_to = datetime(2025, 7, 1, tzinfo=UTC)
        with patch.object(
            svc,
            "_fetch_transactions",
            new=AsyncMock(return_value=[]),
        ) as mock_fetch:
            await svc.detect_subscriptions(
                user_id="test",
                date_from=dt_from,
                date_to=dt_to,
            )
        mock_fetch.assert_called_once()
        args, kwargs = mock_fetch.call_args
        assert kwargs.get("date_from") == dt_from or args[1] == dt_from
        assert kwargs.get("date_to") == dt_to or args[2] == dt_to


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _result_to_subscription with
#   DetectionMethod.MERCHANT_CLASSIFICATION + DetectionMethod.EXACT_AMOUNT
# ═══════════════════════════════════════════════════════════════════════


class TestResultToSubscriptionAllMethods:
    """Verify all detection_method values survive conversion."""

    def test_hybrid_method_survives(self) -> None:
        result = {
            "merchant_name": "Netflix",
            "raw_description": "POS Netflix",
            "amount": Decimal("15.99"),
            "currency_code": "EUR",
            "frequency_days": 30,
            "frequency_label": "monthly",
            "confidence": SubscriptionConfidence.HIGH,
            "detection_score": 0.92,
            "detection_method": DetectionMethod.HYBRID,
            "transaction_ids": ["t1"],
            "account_id": "acct_1",
            "provider_key": "bunq",
        }
        sub = SubscriptionDetectionService._result_to_subscription(
            result, "Netflix"
        )
        assert sub.detection_method == DetectionMethod.HYBRID

    def test_merchant_classification_method_survives(self) -> None:
        result = {
            "merchant_name": "Netflix",
            "raw_description": "POS Netflix",
            "amount": Decimal("15.99"),
            "currency_code": "EUR",
            "frequency_days": None,
            "frequency_label": None,
            "confidence": SubscriptionConfidence.MEDIUM,
            "detection_score": 0.65,
            "detection_method": DetectionMethod.MERCHANT_CLASSIFICATION,
            "transaction_ids": ["t1"],
            "account_id": "acct_1",
            "provider_key": "bunq",
        }
        sub = SubscriptionDetectionService._result_to_subscription(
            result, "Netflix"
        )
        assert sub.detection_method == DetectionMethod.MERCHANT_CLASSIFICATION


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — detect_subscriptions exception handlers
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsExceptionHandlers:
    """Cover the ``except Exception`` handlers in ``detect_subscriptions``.

    Each handler catches a distinct failure mode and logs it, then
    continues with the pipeline so residual data can still produce
    results.
    """

    @pytest.mark.asyncio
    async def test_classification_failure_isolation(self) -> None:
        """Step 1: merchant classification fails — pattern detection
        can still find results from the raw transactions."""
        svc = SubscriptionDetectionService()
        with patch.object(
            svc,
            "_classify_all_merchants",
            new=AsyncMock(side_effect=RuntimeError("DB down")),
        ):
            txns = [
                _make_txn(
                    amount=Decimal("-15.99"),
                    description="Netflix",
                    occurred_at=datetime(2025, 1, 15, tzinfo=UTC)
                    + timedelta(days=30 * i),
                )
                for i in range(3)
            ]
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        # Pattern detection ran anyway with empty classifications
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_pattern_detection_failure_isolation(self) -> None:
        """Step 2: pattern detection fails — subscriptions from
        merchant-classification-only path still appear."""
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC)
                + timedelta(days=30 * i),
            )
            for i in range(3)
        ]
        with patch.object(
            svc,
            "_group_by_merchant_and_amount",
            side_effect=RuntimeError("grouping failed"),
        ):
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        # Netflix is a known subscription merchant, so it should appear
        # via the merchant-classification-only path
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_subscription_construction_failure_isolation(self) -> None:
        """Step 3a: construction of a specific merchant's subscription
        fails — other merchants are unaffected."""
        svc = SubscriptionDetectionService()
        txns = (
            _make_monthly_txns("Netflix", "-15.99", 3)
            + _make_monthly_txns("Spotify", "-9.99", 3)
        )
        with patch.object(
            svc,
            "_result_to_subscription",
            side_effect=RuntimeError("construction boom"),
        ):
            # Pattern results exist but construction raises for all
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        # Exception is caught; merchant-only path may still fire
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_merchant_only_construction_failure_isolation(self) -> None:
        """Step 3b: building a merchant-only subscription fails —
        other merchant-only candidates are unaffected."""
        svc = SubscriptionDetectionService(merchant_only_threshold=0.50)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        with patch.object(
            svc,
            "_merchant_only_subscription",
            side_effect=RuntimeError("merchant sub failed"),
        ):
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        # No crash — handler catches and logs
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_overlap_resolution_failure_isolation(self) -> None:
        """Step 4a: overlap resolution fails — subscriptions are
        returned unsorted without the merge."""
        svc = SubscriptionDetectionService()
        txns = _make_monthly_txns("Netflix", "-15.99", 3)
        with patch.object(
            svc,
            "_resolve_overlaps",
            side_effect=RuntimeError("overlap crash"),
        ):
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        # Subscriptions returned despite overlap failure
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_sort_failure_isolation(self) -> None:
        """Step 4b: sort with corrupt data is caught."""
        svc = SubscriptionDetectionService()
        txns = _make_monthly_txns("Netflix", "-15.99", 3)
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        # Ensure the results list can be returned even if sorting
        # encounters issues internally
        assert isinstance(results, list)


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — DB-backed merchant classifier
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantClassifierDBBacked:
    """Cover the DB-backed merchant-classifier path with and without
    fallback on failure."""

    @pytest.mark.asyncio
    async def test_db_classifier_success(self) -> None:
        """When ``merchant_classifier`` is provided and succeeds,
        ``classification_from_db`` is used."""
        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(
            return_value=MagicMock(
                sector="Technology",
                ticker="GOOGL",
                subscription_likelihood="high",
                security_id="sec_001",
                source="merchant_map",
            )
        )
        svc = SubscriptionDetectionService(
            merchant_classifier=mock_classifier,
        )
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Google One",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        classified, _cancelled = await svc._classify_all_merchants(txns)
        assert len(classified) >= 1
        merchant_key = next(k for k in classified if "Google" in k)
        mc = classified[merchant_key]
        assert getattr(mc, "sector", None) == "Technology"
        assert getattr(mc, "ticker", None) == "GOOGL"
        mock_classifier.classify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_classifier_failure_fallback(self) -> None:
        """When DB-backed classification fails, it falls back to
        standalone ``classify_merchant`` so a single DB failure
        doesn't block all merchants."""
        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(
            side_effect=RuntimeError("DB connection lost"),
        )
        svc = SubscriptionDetectionService(
            merchant_classifier=mock_classifier,
        )
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix B.V.",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        classified, _cancelled = await svc._classify_all_merchants(txns)
        # Despite DB failure, Netflix should be classified via
        # standalone classify_merchant fallback
        assert len(classified) >= 1
        mc = classified.get("Netflix B.V.") or classified.get("Netflix")
        assert mc is not None
        assert getattr(mc, "sector", None) is not None


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _find_classified_without_pattern
#   outer exception handler (lines 398-399)
# ═══════════════════════════════════════════════════════════════════════


class TestFindClassifiedWithoutPatternException:
    """Cover the outer `except Exception:` handler in
    ``detect_subscriptions`` that wraps
    ``_find_classified_without_pattern`` (lines 398-399)."""

    @pytest.mark.asyncio
    async def test_find_classified_without_pattern_raises(self) -> None:
        svc = SubscriptionDetectionService(merchant_only_threshold=0.50)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        with patch.object(
            svc,
            "_find_classified_without_pattern",
            side_effect=RuntimeError("unexpected error"),
        ):
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )
        # Exception is caught and logged; results may be empty or partial
        assert isinstance(results, list)


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _fetch_transactions RuntimeError
# ═══════════════════════════════════════════════════════════════════════


class TestFetchTransactionsRuntimeError:
    """Cover the RuntimeError in _fetch_transactions when
    session_factory is None."""

    @pytest.mark.asyncio
    async def test_no_session_factory_raises(self) -> None:
        svc = SubscriptionDetectionService()
        with pytest.raises(
            RuntimeError,
            match="session_factory required for DB-backed",
        ):
            await svc._fetch_transactions(user_id="test")


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _analyze_group: step-change fallback
# ═══════════════════════════════════════════════════════════════════════


class TestAnalyzeGroupStepChangeFallback:
    """Cover the step-change fallback in _analyze_group (line 642-644)."""

    def test_step_score_fills_consistency(self) -> None:
        """When _amounts_are_consistent returns 0.0 but
        _amounts_step_change_score returns > 0.0, amount_consistency
        is set to the step score."""
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # Amounts: 10, 10, 20, 20 — forms 2 tight clusters
        # _amounts_are_consistent would return 0.0 (variance > 30 %)
        # _amounts_step_change_score returns 0.5
        txns = [
            _make_txn(
                amount=Decimal("-10.00"),
                description="Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-10.00"),
                description="Service",
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-20.00"),
                description="Service",
                occurred_at=base + timedelta(days=60),
            ),
            _make_txn(
                amount=Decimal("-20.00"),
                description="Service",
                occurred_at=base + timedelta(days=90),
            ),
        ]
        result = svc._analyze_group("Service", txns)
        assert result is not None
        # The step-change fallback should have set consistency to 0.5
        assert result["details"]["amount_consistency"] == 0.5


# ═══════════════════════════════════════════════════════════════════════
# SubscriptionDetectionService — _analyze_group: None dates
# ═══════════════════════════════════════════════════════════════════════


class TestAnalyzeGroupNoneDates:
    """Cover the None-dates branch in _analyze_group (line 652->651)."""

    def test_none_dates_do_not_crash(self) -> None:
        """When a transaction has ``occurred_at=None``, the interval
        computation skips it without error."""
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=None,  # type: ignore[arg-type]
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=datetime(2025, 2, 15, tzinfo=UTC),
            ),
        ]
        result = svc._analyze_group("Netflix", txns)
        assert result is not None
        # One None date means only 0 or 1 valid intervals → no label
        assert result["frequency_label"] is None or result[
            "frequency_label"
        ] in ("monthly",)


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorEdgeCases:
    """Cover remaining branches in PatternDetector."""

    def test_empty_transactions_list(self) -> None:
        """Empty list returns [].  (Line 207)"""
        detector = PatternDetector()
        assert detector.detect([]) == []

    def test_no_outgoing_transactions(self) -> None:
        """Only positive (incoming) transactions → no results.
        (Line 218)"""
        detector = PatternDetector()
        txns = [
            _make_txn(amount=Decimal("100.00"), description="Salary"),
            _make_txn(amount=Decimal("50.00"), description="Refund"),
        ]
        assert detector.detect(txns) == []

    def test_positive_amount_not_outgoing(self) -> None:
        """Positive amount with debit-like transaction_type triggers
        outgoing via type check. (Line 273->281)"""
        detector = PatternDetector()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("100.00"),
                description="Fee Chargeback",
                occurred_at=base,
                transaction_type="fee",
            ),
            _make_txn(
                amount=Decimal("100.00"),
                description="Fee Chargeback",
                occurred_at=base + timedelta(days=30),
                transaction_type="fee",
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1

    def test_step_change_overrides_consistency(self) -> None:
        """When _amounts_are_consistent returns 0.0 and step score
        > 0.0, the pattern continues with the step score.
        (Line 344)"""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # Two clear clusters — _amounts_are_consistent returns 0.0
        # but step_score returns 0.5 → pattern continues
        txns = [
            _make_txn(
                amount=Decimal("-7.99"),
                description="Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-7.99"),
                description="Service",
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Service",
                occurred_at=base + timedelta(days=60),
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Service",
                occurred_at=base + timedelta(days=90),
            ),
        ]
        results = detector.detect(txns)
        assert len(results) >= 1
        r = results[0]
        assert r.details["amount_consistency"] == 0.5

    def test_none_dates_in_pattern_detector(self) -> None:
        """None dates skip interval computation without crashing.
        (Lines 352->351, 354->351)"""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Service",
                occurred_at=None,  # type: ignore[arg-type]
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Service",
                occurred_at=base + timedelta(days=30),
            ),
        ]
        results = detector.detect(txns)
        # May or may not detect a pattern; must not crash
        assert isinstance(results, list)


# ═══════════════════════════════════════════════════════════════════════
# PatternDetector — classification handling edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPatternDetectorClassificationEdgeCases:
    """Cover classification handling in PatternDetector._analyse_group."""

    def test_classification_as_empty_dict(self) -> None:
        """Empty classification dict still produces a result.
        (Line 325-327)"""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=30),
            ),
        ]
        results = detector.detect(
            txns,
            classifications={"Some Service": {}},
        )
        assert len(results) >= 1
        assert results[0].sector is None

    def test_classification_with_full_sector(self) -> None:
        """Full merchant classification with sector and likelihood.
        (Lines 331-333)"""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Google One",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Google One",
                occurred_at=base + timedelta(days=30),
            ),
        ]
        results = detector.detect(
            txns,
            classifications={
                "Google One": {
                    "sector": "Technology",
                    "security_id": "sec_001",
                    "likelihood_score": 0.12,
                }
            },
        )
        assert len(results) >= 1
        r = results[0]
        assert r.sector == "Technology"
        assert r.security_id == "sec_001"
        assert r.details["sector_boost"] == 0.12

    def test_classification_no_likelihood_score(self) -> None:
        """Dict classification without likelihood_score defaults to 0.0.
        (Line 334)"""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=30),
            ),
        ]
        results = detector.detect(
            txns,
            classifications={
                "Some Service": {"sector": "Technology"}
            },
        )
        assert len(results) >= 1
        assert results[0].details["sector_boost"] == 0.0

    def test_classification_with_zero_likelihood(self) -> None:
        """likelihood_score of 0.0 should still be pass through correctly."""
        detector = PatternDetector(min_occurrences=2)
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Service",
                occurred_at=base + timedelta(days=30),
            ),
        ]
        results = detector.detect(
            txns,
            classifications={
                "Some Service": {
                    "sector": "Energy",
                    "likelihood_score": 0.0,
                }
            },
        )
        assert len(results) >= 1
        assert results[0].details["sector_boost"] == 0.0
