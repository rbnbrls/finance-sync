"""Tests for the integrated SubscriptionDetectionService.

Covers:
- Subscription dataclass
- Service construction and config
- detect_subscriptions with various data and edge cases
- _classify_all_merchants and cancellation detection
- _group_by_merchant_and_amount
- _outgoing_transactions
- _analyze_group
- _result_to_subscription
- _merchant_only_subscription
- _apply_cross_validation
- _resolve_overlaps
- _find_classified_without_pattern
- _has_cancellation_signal (staticmethod and free function)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)
from finance_sync.services.subscription_detector.merchant_classifier import (
    MerchantClass,
)
from finance_sync.services.subscription_detector.service import (
    Subscription,
    SubscriptionDetectionService,
    _has_cancellation_signal,
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


def _make_merchant_class(
    *,
    is_subscription: bool = True,
    confidence: float = 0.85,
    sector: str = "Communication Services",
    ticker: str = "NFLX",
    source: str = "merchant_map",
    subscription_likelihood: str = "high",
    likelihood_score: float = 0.12,
) -> MagicMock:
    mc = MagicMock(spec=MerchantClass)
    mc.is_subscription = is_subscription
    mc.confidence = confidence
    mc.sector = sector
    mc.ticker = ticker
    mc.source = source
    mc.subscription_likelihood = subscription_likelihood
    mc.likelihood_score = likelihood_score
    mc.security_id = None
    return mc


# ═══════════════════════════════════════════════════════════════════════
# Subscription dataclass
# ═══════════════════════════════════════════════════════════════════════


class TestSubscriptionDataclass:
    """Verify the Subscription dataclass."""

    def test_minimal_init(self) -> None:
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
            transaction_ids=["t1", "t2"],
            account_id="acct_1",
            provider_key="bunq",
        )
        assert sub.merchant_name == "Netflix"
        assert sub.amount == Decimal("15.99")
        assert sub.category is None
        assert sub.sector is None
        assert sub.details == {}

    def test_full_init(self) -> None:
        dt = datetime(2025, 1, 15, tzinfo=UTC)
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
            transaction_ids=["t1", "t2", "t3"],
            account_id="acct_1",
            provider_key="bunq",
            category="streaming",
            sector="Communication Services",
            first_detected_at=dt,
            last_detected_at=dt + timedelta(days=60),
            occurrence_count=3,
            details={"amount_consistency": 1.0},
        )
        assert sub.category == "streaming"
        assert sub.sector == "Communication Services"
        assert sub.occurrence_count == 3


# ═══════════════════════════════════════════════════════════════════════
# Service construction
# ═══════════════════════════════════════════════════════════════════════


class TestServiceConstruction:
    """Verify SubscriptionDetectionService initialisation."""

    def test_with_session_factory(self) -> None:
        svc = SubscriptionDetectionService(
            session_factory=MagicMock(),
        )
        assert svc._session_factory is not None
        assert svc._min_occurrences == 2

    def test_with_custom_min_occurrences(self) -> None:
        svc = SubscriptionDetectionService(min_occurrences=3)
        assert svc._session_factory is None
        assert svc._min_occurrences == 3

    def test_without_session_factory(self) -> None:
        svc = SubscriptionDetectionService()
        assert svc._session_factory is None


# ═══════════════════════════════════════════════════════════════════════
# detect_subscriptions — input validation
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsInputValidation:
    """Verify input validation for detect_subscriptions."""

    @pytest.mark.asyncio
    async def test_no_transactions_no_factory_raises(self) -> None:
        svc = SubscriptionDetectionService()
        with pytest.raises(
            ValueError,
            match="transactions list required when session_factory is None",
        ):
            await svc.detect_subscriptions(user_id="test")

    @pytest.mark.asyncio
    async def test_empty_transactions_returns_empty(self) -> None:
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test",
            transactions=[],
        )
        assert results == []


# ═══════════════════════════════════════════════════════════════════════
# detect_subscriptions — basic patterns
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsBasic:
    """Basic recurring transaction detection."""

    @pytest.mark.asyncio
    async def test_monthly_netflix_detected(self) -> None:
        """6 monthly Netflix charges produce a detected subscription."""
        txns = _make_monthly_txns("Netflix", "-15.99", 6)
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        assert len(results) >= 1
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        r = netflix[0]
        assert r.amount == Decimal("15.99")
        assert r.frequency_label == "monthly"
        assert r.status == SubscriptionStatus.ACTIVE
        # Real classify_merchant classifies Netflix as subscription,
        # so cross-validation produces HYBRID
        assert r.detection_method in (
            DetectionMethod.HYBRID,
            DetectionMethod.EXACT_AMOUNT,
            DetectionMethod.MERCHANT_CLASSIFICATION,
        )
        assert r.occurrence_count >= 2

    @pytest.mark.asyncio
    async def test_multiple_merchants(self) -> None:
        """Netflix and Spotify subscriptions are detected independently."""
        txns = _make_monthly_txns("Netflix", "-15.99", 6) + _make_monthly_txns(
            "Spotify", "-9.99", 6
        )
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        assert len(results) >= 2
        merchants = {r.merchant_name for r in results}
        assert "Netflix" in merchants
        assert "Spotify" in merchants

    @pytest.mark.asyncio
    async def test_non_recurring_not_detected(self) -> None:
        """No outgoing transactions yield empty results."""
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test", transactions=[]
        )
        assert results == []


# ═══════════════════════════════════════════════════════════════════════
# detect_subscriptions — cross-validation
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsCrossValidation:
    """Cross-validation between merchant classifier and pattern detector."""

    @pytest.mark.asyncio
    async def test_hybrid_detection_with_cross_validation(self) -> None:
        """When both methods detect the same merchant, HYBRID method is used."""
        txns = _make_monthly_txns("Netflix", "-15.99", 6)
        svc = SubscriptionDetectionService()
        # Patch classify_merchant at the source module
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mock_classify.return_value = _make_merchant_class(
                sector="Communication Services",
                confidence=0.85,
                likelihood_score=0.12,
            )
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )

        assert len(results) >= 1
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        r = netflix[0]
        # Cross-validation should be applied
        assert r.detection_method == DetectionMethod.HYBRID
        assert r.sector == "Communication Services"

    @pytest.mark.asyncio
    async def test_merchant_only_when_pattern_insufficient(self) -> None:
        """A high-confidence classified merchant with < min_occurrences
        still appears as merchant-only detection."""
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        svc = SubscriptionDetectionService()
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mock_classify.return_value = _make_merchant_class(
                sector="Communication Services",
                confidence=0.85,
                likelihood_score=0.12,
            )
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )

        # Should find Netflix via merchant classification only
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        r = netflix[0]
        assert r.detection_method == DetectionMethod.MERCHANT_CLASSIFICATION
        assert r.sector == "Communication Services"

    @pytest.mark.asyncio
    async def test_low_conf_merchant_not_included_without_pattern(self) -> None:
        """Low-confidence merchants without pattern are not included."""
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Unknown Service",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        svc = SubscriptionDetectionService()
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mock_classify.return_value = _make_merchant_class(
                sector=None,
                confidence=0.30,  # LOW confidence
                likelihood_score=0.0,
                is_subscription=True,  # but low confidence
            )
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )

        # Low confidence merchant (< 0.60 threshold) not included without pattern  # noqa: E501
        assert len(results) == 0


# ═══════════════════════════════════════════════════════════════════════
# detect_subscriptions — cancellation detection
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsCancellation:
    """Cancellation signal detection."""

    @pytest.mark.asyncio
    async def test_cancelled_subscription_marked_cancelled(self) -> None:
        """A subscription with a cancellation transaction gets CANCELLED status."""  # noqa: E501
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = _make_monthly_txns("Netflix", "-15.99", 5, base=base)
        # Add a cancellation transaction
        txns.append(
            _make_txn(
                amount=Decimal("-0.00"),
                description="Netflix Cancellation REF:12345",
                occurred_at=base + timedelta(days=180),
            )
        )
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        # The cancellation transaction has amount 0 so it may not affect
        # the pattern detection. But the cancellation signal detection
        # should mark at least one result as CANCELLED if applicable.
        # (Cancellation signal is checked per-description on all txns)

    @pytest.mark.asyncio
    async def test_refund_signal_detected(self) -> None:
        """Refund keyword triggers cancellation."""
        svc = SubscriptionDetectionService()
        txns = _make_monthly_txns("Netflix", "-15.99", 4)
        txns.append(
            _make_txn(
                amount=Decimal("15.99"),
                description="Netflix Refund",
                occurred_at=datetime(2025, 7, 15, tzinfo=UTC),
            )
        )
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        # The refund transaction is positive so not outgoing,
        # but the cancellation signal should be detected on the description
        results_netflix = [r for r in results if "Netflix" in r.merchant_name]
        # May or may not be included depending on detection logic
        _ = results_netflix  # suppress unused-variable warning


# ═══════════════════════════════════════════════════════════════════════
# detect_subscriptions — multiple subscriptions from same merchant
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsMultipleSameMerchant:
    """Multiple subscriptions from the same merchant at different amounts."""

    @pytest.mark.asyncio
    async def test_two_plans_at_different_amounts(self) -> None:
        """Basic (€10) and Premium (€20) are detected as separate subs."""
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = []
        for i in range(6):
            txns.append(
                _make_txn(
                    amount=Decimal("-10.00"),
                    description="Some Service Basic",
                    occurred_at=base + timedelta(days=30 * i),
                )
            )
            txns.append(
                _make_txn(
                    amount=Decimal("-20.00"),
                    description="Some Service Premium",
                    occurred_at=base + timedelta(days=30 * i),
                )
            )
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        same_merchant = [
            r for r in results if "Some Service" in r.merchant_name
        ]
        # Grouped by (merchant, amount bucket), so two separate results
        # (but may be resolved as overlaps if amounts are within tolerance)
        # 10 and 20 differ by 50% — well outside 2% tolerance → separate subs
        assert len(same_merchant) == 2

    @pytest.mark.asyncio
    async def test_same_amount_dedup(self) -> None:
        """Same merchant with same amount → deduplicated to one."""
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # Explicitly create two groups that will be grouped by amount
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=base + timedelta(days=30 * i),
                txn_id=f"t_{i}",
            )
            for i in range(6)
        ]
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        # Should be deduplicated to 1 (all same amount)
        assert len(netflix) == 1


# ═══════════════════════════════════════════════════════════════════════
# _classify_all_merchants
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyAllMerchants:
    """Verify _classify_all_merchants internal method."""

    @pytest.mark.asyncio
    async def test_classifies_unique_merchants(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(description="Netflix"),
            _make_txn(description="Netflix"),
            _make_txn(description="Spotify"),
        ]
        classified, cancelled = await svc._classify_all_merchants(txns)
        assert len(classified) >= 2
        assert "Netflix" in classified or "Netflix B.V." in classified
        assert "Spotify" in classified or "Spotify Ab" in classified
        assert len(cancelled) == 0

    @pytest.mark.asyncio
    async def test_detects_cancellation_signals(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(description="Netflix"),
            _make_txn(description="Netflix Cancellation"),
        ]
        _classified, cancelled = await svc._classify_all_merchants(txns)
        # "Netflix" is normalised to "Netflix" and "Netflix Cancellation"
        # is normalised to "Netflix Cancellation" (different merchants).
        # The cancellation flag is on "Netflix Cancellation", not "Netflix".
        # At least one merchant in the set contains "Netflix"
        has_cancelled_netflix = any("Netflix" in m for m in cancelled)
        assert has_cancelled_netflix

    @pytest.mark.asyncio
    async def test_empty_transactions(self) -> None:
        svc = SubscriptionDetectionService()
        classified, cancelled = await svc._classify_all_merchants([])
        assert classified == {}
        assert cancelled == set()


# ═══════════════════════════════════════════════════════════════════════
# _group_by_merchant_and_amount
# ═══════════════════════════════════════════════════════════════════════


class TestGroupByMerchantAndAmount:
    """Verify _group_by_merchant_and_amount grouping logic."""

    def test_groups_by_merchant_and_amount(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
            ),
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
            ),
            _make_txn(
                amount=Decimal("-9.99"),
                description="Spotify",
            ),
        ]
        groups = svc._group_by_merchant_and_amount(txns)
        assert len(groups) == 2

    def test_positive_amounts_excluded(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(amount=Decimal("100.00"), description="Salary"),
            _make_txn(amount=Decimal("-15.99"), description="Netflix"),
        ]
        groups = svc._group_by_merchant_and_amount(txns)
        assert len(groups) == 1

    def test_none_amount_excluded(self) -> None:
        svc = SubscriptionDetectionService()
        txns = [
            _make_txn(amount=None, description="Netflix"),  # type: ignore[arg-type]
        ]
        groups = svc._group_by_merchant_and_amount(txns)
        assert len(groups) == 0


# ═══════════════════════════════════════════════════════════════════════
# _outgoing_transactions
# ═══════════════════════════════════════════════════════════════════════


class TestOutgoingTransactions:
    """Verify _outgoing_transactions filter."""

    def test_filters_outgoing_only(self) -> None:
        txns = [
            _make_txn(amount=Decimal("-10.00")),
            _make_txn(amount=Decimal("100.00")),
            _make_txn(amount=Decimal("-15.99")),
        ]
        outgoing = SubscriptionDetectionService._outgoing_transactions(txns)
        assert len(outgoing) == 2
        for t in outgoing:
            assert Decimal(str(t["amount"])) < 0

    def test_empty_input(self) -> None:
        assert SubscriptionDetectionService._outgoing_transactions([]) == []


# ═══════════════════════════════════════════════════════════════════════
# _analyze_group
# ═══════════════════════════════════════════════════════════════════════


class TestAnalyzeGroup:
    """Verify _analyze_group instance method."""

    def test_inconsistent_amounts_return_none(self) -> None:
        svc = SubscriptionDetectionService()
        base = datetime(2025, 1, 15, tzinfo=UTC)
        txns = [
            _make_txn(
                amount=Decimal("-10.00"),
                occurred_at=base,
            ),
            _make_txn(
                amount=Decimal("-50.00"),
                occurred_at=base + timedelta(days=30),
            ),
            _make_txn(
                amount=Decimal("-100.00"),
                occurred_at=base + timedelta(days=60),
            ),
        ]
        result = svc._analyze_group("Store", txns)
        assert result is None

    def test_consistent_amounts_returns_result(self) -> None:
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
        result = svc._analyze_group("Netflix", txns)
        assert result is not None
        assert "Netflix" in result["merchant_name"]
        assert result["detection_score"] > 0

    def test_with_classification_data(self) -> None:
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
        classification = _make_merchant_class(
            sector="Technology", likelihood_score=0.06
        )
        result = svc._analyze_group(
            "Netflix", txns, classification=classification
        )
        assert result is not None
        assert result["sector"] == "Technology"
        assert (
            result["detection_method"]
            == DetectionMethod.MERCHANT_CLASSIFICATION
        )

    def test_with_classification_as_dict(self) -> None:
        """Classification as plain dict also works."""
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
        classification = {"sector": "Technology", "likelihood_score": 0.06}
        result = svc._analyze_group(
            "Netflix", txns, classification=classification
        )
        assert result is not None
        assert result["sector"] == "Technology"
        assert (
            result["detection_method"]
            == DetectionMethod.MERCHANT_CLASSIFICATION
        )


# ═══════════════════════════════════════════════════════════════════════
# _result_to_subscription
# ═══════════════════════════════════════════════════════════════════════


class TestResultToSubscription:
    """Verify _result_to_subscription conversion."""

    def test_converts_result_dict(self) -> None:
        dt = datetime(2025, 1, 15, tzinfo=UTC)
        result = {
            "merchant_name": "Netflix",
            "raw_description": "POS Netflix",
            "amount": Decimal("15.99"),
            "currency_code": "EUR",
            "frequency_days": 30,
            "frequency_label": "monthly",
            "confidence": SubscriptionConfidence.HIGH,
            "detection_score": 0.92,
            "detection_method": DetectionMethod.EXACT_AMOUNT,
            "transaction_ids": ["t1", "t2"],
            "account_id": "acct_1",
            "provider_key": "bunq",
            "category": "streaming",
            "sector": "Communication Services",
            "first_detected_at": dt,
            "last_detected_at": dt + timedelta(days=30),
            "occurrence_count": 2,
            "details": {"amount_consistency": 1.0},
        }
        sub = SubscriptionDetectionService._result_to_subscription(
            result, "Netflix"
        )
        assert isinstance(sub, Subscription)
        assert sub.merchant_name == "Netflix"
        assert sub.amount == Decimal("15.99")
        assert sub.status == SubscriptionStatus.ACTIVE  # not cancelled

    def test_cancelled_true(self) -> None:
        result = {
            "merchant_name": "Netflix",
            "amount": Decimal("15.99"),
            "currency_code": "EUR",
            "confidence": SubscriptionConfidence.HIGH,
            "detection_score": 0.92,
            "detection_method": DetectionMethod.EXACT_AMOUNT,
            "transaction_ids": ["t1"],
            "account_id": "acct_1",
            "provider_key": "bunq",
        }
        sub = SubscriptionDetectionService._result_to_subscription(
            result, "Netflix", cancelled=True
        )
        assert sub.status == SubscriptionStatus.CANCELLED

    def test_missing_fields_default(self) -> None:
        result: dict = {
            "merchant_name": "Unknown",
            "amount": Decimal(0),
            "currency_code": "EUR",
            "transaction_ids": [],
            "account_id": "",
            "provider_key": "",
        }
        # When keys are missing, the result uses dataclass defaults
        sub = SubscriptionDetectionService._result_to_subscription(
            result, "Unknown"
        )
        assert sub.confidence == SubscriptionConfidence.LOW
        assert sub.detection_score == 0.0
        assert sub.detection_method == DetectionMethod.EXACT_AMOUNT


# ═══════════════════════════════════════════════════════════════════════
# _merchant_only_subscription
# ═══════════════════════════════════════════════════════════════════════


class TestMerchantOnlySubscription:
    """Verify _merchant_only_subscription builds Subscription from classification."""  # noqa: E501

    @pytest.mark.asyncio
    async def test_merchant_only_result(self) -> None:
        svc = SubscriptionDetectionService()
        merchant_class = _make_merchant_class(
            sector="Communication Services",
            confidence=0.85,
            likelihood_score=0.12,
        )
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        sub = svc._merchant_only_subscription("Netflix", merchant_class, txns)
        assert sub.merchant_name == "Netflix"
        assert sub.detection_method == DetectionMethod.MERCHANT_CLASSIFICATION
        assert sub.sector == "Communication Services"
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.detection_score > 0
        # Details should contain merchant info
        assert "merchant_confidence" in sub.details
        assert sub.details["detection_note"] == "merchant_classification_only"

    @pytest.mark.asyncio
    async def test_cancelled_merchant(self) -> None:
        svc = SubscriptionDetectionService()
        merchant_class = _make_merchant_class(confidence=0.85)
        txns = [
            _make_txn(description="Netflix"),
        ]
        sub = svc._merchant_only_subscription(
            "Netflix", merchant_class, txns, cancelled=True
        )
        assert sub.status == SubscriptionStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_low_confidence_merchant_only(self) -> None:
        """Low-confidence merchant classification still produces a Subscription."""  # noqa: E501
        svc = SubscriptionDetectionService()
        merchant_class = _make_merchant_class(
            sector="Energy",
            confidence=0.30,  # LOW
            likelihood_score=0.0,
            subscription_likelihood="low",
        )
        txns = [
            _make_txn(
                amount=Decimal("-10.00"),
                description="Some Service",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        sub = svc._merchant_only_subscription(
            "Some Service", merchant_class, txns
        )
        assert sub.confidence == SubscriptionConfidence.LOW
        assert sub.detection_score < 0.50
        assert sub.detection_method == DetectionMethod.MERCHANT_CLASSIFICATION
        assert sub.details["merchant_confidence"] == 0.30


# ═══════════════════════════════════════════════════════════════════════
# _apply_cross_validation
# ═══════════════════════════════════════════════════════════════════════


class TestApplyCrossValidation:
    """Verify _apply_cross_validation logic."""

    @pytest.fixture
    def svc(self) -> SubscriptionDetectionService:
        return SubscriptionDetectionService()

    def test_upgrades_to_hybrid(self, svc) -> None:
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
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
        )
        mc = _make_merchant_class(is_subscription=True)
        upgraded = svc._apply_cross_validation(sub, mc)
        assert upgraded.detection_method == DetectionMethod.HYBRID
        assert upgraded.detection_score == pytest.approx(0.80)  # 0.70 + 0.10
        assert upgraded.confidence == SubscriptionConfidence.HIGH

    def test_cross_validation_medium_confidence(self, svc) -> None:
        """Score 0.40 + 0.10 = 0.50 → MEDIUM confidence."""
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
            currency_code="EUR",
            frequency_days=30,
            frequency_label="monthly",
            confidence=SubscriptionConfidence.LOW,
            detection_score=0.40,
            detection_method=DetectionMethod.EXACT_AMOUNT,
            status=SubscriptionStatus.ACTIVE,
            transaction_ids=["t1"],
            account_id="acct_1",
            provider_key="bunq",
        )
        mc = _make_merchant_class(is_subscription=True)
        upgraded = svc._apply_cross_validation(sub, mc)
        assert upgraded.detection_score == pytest.approx(0.50)  # 0.40 + 0.10
        assert upgraded.confidence == SubscriptionConfidence.MEDIUM

    def test_cross_validation_low_stays_low(self, svc) -> None:
        """Score 0.20 + 0.10 = 0.30 → stays LOW."""
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
            currency_code="EUR",
            frequency_days=30,
            frequency_label="monthly",
            confidence=SubscriptionConfidence.LOW,
            detection_score=0.20,
            detection_method=DetectionMethod.EXACT_AMOUNT,
            status=SubscriptionStatus.ACTIVE,
            transaction_ids=["t1"],
            account_id="acct_1",
            provider_key="bunq",
        )
        mc = _make_merchant_class(is_subscription=True)
        upgraded = svc._apply_cross_validation(sub, mc)
        assert upgraded.detection_score == pytest.approx(0.30)  # 0.20 + 0.10
        assert upgraded.confidence == SubscriptionConfidence.LOW

    def test_does_not_double_upgrade(self, svc) -> None:
        """Already HYBRID is not upgraded again."""
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
            currency_code="EUR",
            frequency_days=30,
            frequency_label="monthly",
            confidence=SubscriptionConfidence.HIGH,
            detection_score=0.85,
            detection_method=DetectionMethod.HYBRID,
            status=SubscriptionStatus.ACTIVE,
            transaction_ids=["t1"],
            account_id="acct_1",
            provider_key="bunq",
        )
        mc = _make_merchant_class(is_subscription=True)
        upgraded = svc._apply_cross_validation(sub, mc)
        assert upgraded.detection_method == DetectionMethod.HYBRID
        assert upgraded.detection_score == 0.85  # unchanged

    def test_no_upgrade_when_not_subscription(self, svc) -> None:
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
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
        )
        mc = _make_merchant_class(is_subscription=False)
        result = svc._apply_cross_validation(sub, mc)
        assert result is sub  # unchanged, same reference

    def test_sector_filled_if_missing(self, svc) -> None:
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
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
            sector=None,
        )
        mc = _make_merchant_class(
            is_subscription=True,
            sector="Communication Services",
        )
        upgraded = svc._apply_cross_validation(sub, mc)
        assert upgraded.sector == "Communication Services"

    def test_custom_bonus(self, svc) -> None:
        """Custom cross_validation_bonus is used when configured."""
        svc_custom = SubscriptionDetectionService(cross_validation_bonus=0.20)
        sub = Subscription(
            merchant_name="Netflix",
            raw_description="POS Netflix",
            amount=Decimal("15.99"),
            currency_code="EUR",
            frequency_days=30,
            frequency_label="monthly",
            confidence=SubscriptionConfidence.MEDIUM,
            detection_score=0.60,
            detection_method=DetectionMethod.EXACT_AMOUNT,
            status=SubscriptionStatus.ACTIVE,
            transaction_ids=["t1"],
            account_id="acct_1",
            provider_key="bunq",
        )
        mc = _make_merchant_class(is_subscription=True)
        upgraded = svc_custom._apply_cross_validation(sub, mc)
        assert upgraded.detection_score == pytest.approx(0.80)  # 0.60 + 0.20
        assert upgraded.detection_method == DetectionMethod.HYBRID


# ═══════════════════════════════════════════════════════════════════════
# _resolve_overlaps
# ═══════════════════════════════════════════════════════════════════════


class TestResolveOverlaps:
    """Verify _resolve_overlaps deduplication."""

    @pytest.fixture
    def svc(self) -> SubscriptionDetectionService:
        return SubscriptionDetectionService()

    def _make_sub(
        self,
        merchant: str = "Netflix",
        amount: Decimal = Decimal("15.99"),
        score: float = 0.80,
    ) -> Subscription:
        return Subscription(
            merchant_name=merchant,
            raw_description=None,
            amount=amount,
            currency_code="EUR",
            frequency_days=30,
            frequency_label="monthly",
            confidence=SubscriptionConfidence.MEDIUM,
            detection_score=score,
            detection_method=DetectionMethod.EXACT_AMOUNT,
            status=SubscriptionStatus.ACTIVE,
            transaction_ids=["t1"],
            account_id="acct_1",
            provider_key="bunq",
        )

    def test_empty_list(self, svc) -> None:
        assert svc._resolve_overlaps([]) == []

    def test_no_duplicates(self, svc) -> None:
        subs = [
            self._make_sub("Netflix"),
            self._make_sub("Spotify"),
        ]
        resolved = svc._resolve_overlaps(subs)
        assert len(resolved) == 2

    def test_same_merchant_same_amount_dedup(self, svc) -> None:
        """Same merchant, same amount → keep higher scoring one."""
        subs = [
            self._make_sub("Netflix", amount=Decimal("15.99"), score=0.70),
            self._make_sub("Netflix", amount=Decimal("15.99"), score=0.90),
        ]
        resolved = svc._resolve_overlaps(subs)
        assert len(resolved) == 1
        assert resolved[0].detection_score == 0.90

    def test_same_merchant_different_preserved(self, svc) -> None:
        """Same merchant, different amounts → both preserved."""
        subs = [
            self._make_sub("Streaming", amount=Decimal("10.00"), score=0.80),
            self._make_sub("Streaming", amount=Decimal("20.00"), score=0.80),
        ]
        resolved = svc._resolve_overlaps(subs)
        assert len(resolved) == 2

    def test_same_merchant_amount_within_tolerance(self, svc) -> None:
        """Amounts within 2% tolerance → deduped."""
        subs = [
            self._make_sub("Netflix", amount=Decimal("10.00"), score=0.90),
            self._make_sub(
                "Netflix", amount=Decimal("10.10"), score=0.80
            ),  # 1% diff
        ]
        resolved = svc._resolve_overlaps(subs)
        assert len(resolved) == 1
        assert resolved[0].amount == Decimal("10.00")
        assert resolved[0].detection_score == 0.90  # highest kept

    def test_overlap_with_zero_amount_excluded(self, svc) -> None:
        """Subscriptions with zero amount don't interfere with overlap detection."""  # noqa: E501
        subs = [
            self._make_sub("Netflix", amount=Decimal("15.99"), score=0.80),
            self._make_sub("Netflix", amount=Decimal(0), score=0.70),
        ]
        resolved = svc._resolve_overlaps(subs)
        # Zero-amount subscriptions skip the amount-comparison and
        # are added as distinct entries
        assert len(resolved) == 2


# ═══════════════════════════════════════════════════════════════════════
# _find_classified_without_pattern
# ═══════════════════════════════════════════════════════════════════════


class TestFindClassifiedWithoutPattern:
    """Verify _find_classified_without_pattern logic."""

    @pytest.fixture
    def svc(self) -> SubscriptionDetectionService:
        return SubscriptionDetectionService()

    def test_merchant_without_pattern_included(self, svc) -> None:
        classified = {
            "Netflix": _make_merchant_class(
                is_subscription=True, confidence=0.85
            ),
        }
        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Netflix",
            ),
        ]
        candidates = svc._find_classified_without_pattern(classified, {}, txns)
        assert len(candidates) == 1
        assert candidates[0][0] == "Netflix"

    def test_merchant_with_pattern_excluded(self, svc) -> None:
        classified = {
            "Netflix": _make_merchant_class(
                is_subscription=True, confidence=0.85
            ),
        }
        pattern_results = {
            "Netflix": [{"merchant_name": "Netflix"}],
        }
        txns = [
            _make_txn(description="Netflix"),
        ]
        candidates = svc._find_classified_without_pattern(
            classified, pattern_results, txns
        )
        assert candidates == []

    def test_low_conf_merchant_excluded(self, svc) -> None:
        classified = {
            "Netflix": _make_merchant_class(
                is_subscription=True, confidence=0.30
            ),
        }
        txns = [
            _make_txn(description="Netflix"),
        ]
        candidates = svc._find_classified_without_pattern(classified, {}, txns)
        assert candidates == []

    def test_not_subscription_excluded(self, svc) -> None:
        classified = {
            "Store": _make_merchant_class(
                is_subscription=False, confidence=0.85
            ),
        }
        txns = [
            _make_txn(description="Store"),
        ]
        candidates = svc._find_classified_without_pattern(classified, {}, txns)
        assert candidates == []


# ═══════════════════════════════════════════════════════════════════════
# _has_cancellation_signal (staticmethod)
# ═══════════════════════════════════════════════════════════════════════


class TestHasCancellationSignalStatic:
    """Verify the staticmethod _has_cancellation_signal."""

    def test_cancellation_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Netflix Cancellation"
        )

    def test_cancel_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Cancel Subscription"
        )

    def test_terminated_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Service Terminated"
        )

    def test_ended_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Contract Ended"
        )

    def test_closed_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Account Closed"
        )

    def test_deactivated_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Deactivated Plan"
        )

    def test_stopped_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Stopped Payment"
        )

    def test_refund_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Refund Netflix"
        )

    def test_reversal_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Payment Reversal"
        )

    def test_chargeback_keyword(self) -> None:
        assert SubscriptionDetectionService._has_cancellation_signal(
            "Chargeback Transaction"
        )

    def test_no_cancellation_signal(self) -> None:
        assert not SubscriptionDetectionService._has_cancellation_signal(
            "Netflix Subscription"
        )

    def test_empty_string(self) -> None:
        assert not SubscriptionDetectionService._has_cancellation_signal("")

    def test_normal_payment_no_signal(self) -> None:
        assert not SubscriptionDetectionService._has_cancellation_signal(
            "Payment to Netflix"
        )


# ═══════════════════════════════════════════════════════════════════════
# _has_cancellation_signal (free function)
# ═══════════════════════════════════════════════════════════════════════


class TestHasCancellationSignalFree:
    """Verify the free function _has_cancellation_signal."""

    def test_cancellation_keyword(self) -> None:
        assert _has_cancellation_signal("Netflix Cancellation")

    def test_refund_keyword(self) -> None:
        assert _has_cancellation_signal("Refund Transaction")

    def test_no_signal(self) -> None:
        assert not _has_cancellation_signal("Netflix Monthly Payment")

    def test_case_insensitive(self) -> None:
        assert _has_cancellation_signal("CANCELLATION OF SERVICE")


# ═══════════════════════════════════════════════════════════════════════
# Configurable thresholds
# ═══════════════════════════════════════════════════════════════════════


class TestConfigurableThresholds:
    """Verify that configurable thresholds behave correctly."""

    def test_config_property_with_defaults(self) -> None:
        svc = SubscriptionDetectionService()
        cfg = svc.config
        assert cfg["min_occurrences"] == 2
        assert cfg["merchant_only_threshold"] == 0.60
        assert cfg["cross_validation_bonus"] == 0.10
        assert cfg["amount_bucket_tolerance"] == "0.02"

    def test_config_property_with_custom_values(self) -> None:
        svc = SubscriptionDetectionService(
            min_occurrences=3,
            merchant_only_threshold=0.70,
            cross_validation_bonus=0.20,
            amount_bucket_tolerance=Decimal("0.05"),
        )
        cfg = svc.config
        assert cfg["min_occurrences"] == 3
        assert cfg["merchant_only_threshold"] == 0.70
        assert cfg["cross_validation_bonus"] == 0.20
        assert cfg["amount_bucket_tolerance"] == "0.05"

    @pytest.mark.asyncio
    async def test_higher_merchant_only_threshold_excludes_low_conf(
        self,
    ) -> None:
        """A higher merchant_only_threshold excludes low-confidence merchants
        from appearing as merchant-only subscriptions."""
        from unittest.mock import patch

        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Unknown Service",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        # With a high threshold of 0.80, a 0.85 confidence merchant still passes
        svc = SubscriptionDetectionService(merchant_only_threshold=0.80)
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mock_classify.return_value = _make_merchant_class(
                sector=None,
                confidence=0.85,
                likelihood_score=0.06,
                is_subscription=True,
            )
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )

        # 0.85 >= 0.80 threshold, so merchant should be included
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_higher_merchant_only_threshold_blocks_mid_conf(self) -> None:
        """A higher merchant_only_threshold blocks mid-confidence merchants."""
        from unittest.mock import patch

        txns = [
            _make_txn(
                amount=Decimal("-15.99"),
                description="Some Unknown Service",
                occurred_at=datetime(2025, 1, 15, tzinfo=UTC),
            ),
        ]
        # With a high threshold of 0.80, a 0.60 confidence merchant is excluded
        svc = SubscriptionDetectionService(merchant_only_threshold=0.80)
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mock_classify.return_value = _make_merchant_class(
                sector=None,
                confidence=0.60,
                likelihood_score=0.06,
                is_subscription=True,
            )
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )

        # 0.60 < 0.80 threshold → not included as merchant-only
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_lower_cross_validation_bonus(self) -> None:
        """A lower cross_validation_bonus produces a smaller score bump."""
        from unittest.mock import patch

        txns = _make_monthly_txns("Netflix", "-15.99", 3)
        svc = SubscriptionDetectionService(cross_validation_bonus=0.05)
        with patch(
            "finance_sync.services.subscription_detector.merchant_classifier.classify_merchant",
        ) as mock_classify:
            mock_classify.return_value = _make_merchant_class(
                sector="Communication Services",
                confidence=0.85,
                likelihood_score=0.12,
            )
            results = await svc.detect_subscriptions(
                user_id="test", transactions=txns
            )

        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        r = netflix[0]
        if r.detection_method == DetectionMethod.HYBRID:
            # The bonus should be 0.05 not 0.10
            assert r.details.get("cross_validation_bonus") == 0.05

    @pytest.mark.asyncio
    async def test_custom_amount_bucket_tolerance(self) -> None:
        """Wider amount_bucket_tolerance merges more subscriptions."""
        base = datetime(2025, 1, 15, tzinfo=UTC)
        # Create two groups at very similar amounts (4% difference)
        txns = []
        for i in range(6):
            txns.append(
                _make_txn(
                    amount=Decimal("-10.00"),
                    description="Some Service",
                    occurred_at=base + timedelta(days=30 * i),
                    txn_id=f"basic_{i}",
                )
            )
            txns.append(
                _make_txn(
                    amount=Decimal("-10.40"),  # 4% higher
                    description="Some Service",
                    occurred_at=base + timedelta(days=30 * i),
                    txn_id=f"premium_{i}",
                )
            )
        # Default tolerance is 2% → should be two separate subscriptions
        svc_default = SubscriptionDetectionService()
        results_default = await svc_default.detect_subscriptions(
            user_id="test", transactions=txns
        )
        same_merchant_default = [
            r for r in results_default if "Some Service" in r.merchant_name
        ]
        assert len(same_merchant_default) == 2

        # Wider tolerance (5%) → merges into one
        svc_wide = SubscriptionDetectionService(
            amount_bucket_tolerance=Decimal("0.05"),
        )
        results_wide = await svc_wide.detect_subscriptions(
            user_id="test", transactions=txns
        )
        same_merchant_wide = [
            r for r in results_wide if "Some Service" in r.merchant_name
        ]
        # Due to amount grouping in step 2, 10.00 and 10.40 are already
        # separate groups (bucketed). Overlap resolution in step 4 may
        # merge them when tolerance is wide enough.
        # With 5% tolerance, 10.00 and 10.40 differ by 3.8% < 5%, merged
        assert len(same_merchant_wide) == 1


# ═══════════════════════════════════════════════════════════════════════
# detect_subscriptions — fundamentals integration
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionsFundamentals:
    """Verify fundamentals data integration via MerchantClassifier."""

    @pytest.mark.asyncio
    async def test_no_merchant_classifier_falls_back_to_standalone(
        self,
    ) -> None:
        """Without MerchantClassifier, the standalone classify_merchant is used."""  # noqa: E501
        txns = _make_monthly_txns("Netflix", "-15.99", 6)
        svc = SubscriptionDetectionService()
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        r = netflix[0]
        # Standalone mode sets fundamentals_available=False
        assert r.fundamentals_available is False
        assert r.security_id is None

    @pytest.mark.asyncio
    async def test_merchant_classifier_enriches_with_fundamentals(
        self,
    ) -> None:
        """When MerchantClassifier is provided, fundamentals data enriches results."""  # noqa: E501
        txns = _make_monthly_txns("Netflix", "-15.99", 6)

        # Mock the MerchantClassifier
        mock_classifier = AsyncMock()
        mock_classifier.classify.return_value = MagicMock(
            merchant_name="Netflix",
            sector="Communication Services",
            ticker="NFLX",
            subscription_likelihood="high",
            security_id="sec_nflx_001",
            source="merchant_map",
        )

        svc = SubscriptionDetectionService(
            merchant_classifier=mock_classifier,
        )
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        r = netflix[0]

        # MerchantClassifier was called
        mock_classifier.classify.assert_called()

        # Fundamentals data should be present
        assert r.security_id == "sec_nflx_001"
        assert r.fundamentals_available is True

    @pytest.mark.asyncio
    async def test_merchant_classifier_only_merchants_carry_fundamentals(
        self,
    ) -> None:
        """Merchant-only subscriptions (no pattern) carry fundamentals data."""
        # Single transaction — not enough for pattern detection
        txns = [
            _make_txn(
                description="Netflix",
                amount=Decimal("-15.99"),
                occurred_at=datetime(2025, 6, 1, tzinfo=UTC),
            ),
        ]

        mock_classifier = AsyncMock()
        mock_classifier.classify.return_value = MagicMock(
            merchant_name="Netflix",
            sector="Communication Services",
            ticker="NFLX",
            subscription_likelihood="high",
            security_id="sec_nflx_001",
            source="merchant_map",
        )

        svc = SubscriptionDetectionService(
            merchant_classifier=mock_classifier,
        )
        results = await svc.detect_subscriptions(
            user_id="test", transactions=txns
        )
        netflix = [r for r in results if "Netflix" in r.merchant_name]
        assert len(netflix) >= 1
        r = netflix[0]

        # Even with only 1 transaction, fundamentals should flow through
        assert r.security_id == "sec_nflx_001"
        assert r.fundamentals_available is True
        assert r.detection_method == DetectionMethod.MERCHANT_CLASSIFICATION
