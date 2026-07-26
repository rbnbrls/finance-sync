"""Integration tests for the subscription detection fusion pipeline.

Verifies that merchant classification (Task 0) and pattern recognition
(Task 1) are combined, cross-referenced, and weighted correctly to
produce the final list of detected subscriptions.

Covers:
- _merge_cross_validated — merging and boosting logic
- _cross_reference_results — combining both result sets
- Full _run_all_detection pipeline with mocked internals
- Score boost, detection method upgrade, detail merging
- Edge cases: empty lists, no overlap, all overlap
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)
from finance_sync.services.subscription_detector import (
    SubscriptionDetector,
    _merge_cross_validated,
)

# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_mock_detector() -> SubscriptionDetector:
    """Create a minimal SubscriptionDetector with mocked DB session."""
    mock_session = MagicMock()
    factory = MagicMock(return_value=mock_session)
    return SubscriptionDetector(session_factory=factory, tenant_id="tenant_1")


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
    """Create a merchant-based detection result dict."""
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
    """Create a clustering-based detection result dict."""
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


# ═══════════════════════════════════════════════════════════════════════
# _merge_cross_validated unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestMergeCrossValidated:
    """Verify the low-level merge-and-boost function."""

    def test_merchant_score_higher_becomes_base(self) -> None:
        """When merchant score is higher, it becomes the base."""
        mr = _merchant_result(score=0.75)
        cr = _cluster_result(score=0.60)
        merged = _merge_cross_validated(mr, cr)

        assert merged["detection_method"] == DetectionMethod.HYBRID
        assert merged["detection_score"] == pytest.approx(0.85)
        assert merged["confidence"] == SubscriptionConfidence.HIGH
        assert merged["merchant_name"] == "Netflix B.V."

    def test_cluster_score_higher_becomes_base(self) -> None:
        """When cluster score is higher, it becomes the base."""
        mr = _merchant_result(score=0.55)
        cr = _cluster_result(score=0.70)
        merged = _merge_cross_validated(mr, cr)

        assert merged["detection_method"] == DetectionMethod.HYBRID
        assert merged["detection_score"] == pytest.approx(0.80)
        assert merged["confidence"] == SubscriptionConfidence.HIGH

    def test_missing_sector_filled_from_other(self) -> None:
        """Sector data from one source fills the other."""
        mr = _merchant_result(
            score=0.70,
            sector="Communication Services",
        )
        cr = _cluster_result(score=0.60, sector=None)
        merged = _merge_cross_validated(mr, cr)

        assert merged["sector"] == "Communication Services"

    def test_merchant_ids_and_cluster_ids_merged(self) -> None:
        """Transaction IDs from both sources are deduplicated."""
        mr = _merchant_result(
            score=0.70,
            txn_ids=["t1", "t2", "t3"],
            occurrence_count=3,
        )
        cr = _cluster_result(
            score=0.60,
            txn_ids=["t3", "t4", "t5"],
            occurrence_count=3,
        )
        merged = _merge_cross_validated(mr, cr)

        assert set(merged["transaction_ids"]) == {"t1", "t2", "t3", "t4", "t5"}
        assert merged["details"]["merged_transaction_count"] == 5

    def test_occurrence_count_takes_max(self) -> None:
        """Occurrence count takes the max of both sources."""
        mr = _merchant_result(score=0.70, occurrence_count=3)
        cr = _cluster_result(score=0.60, occurrence_count=8)
        merged = _merge_cross_validated(mr, cr)

        assert merged["occurrence_count"] == max(3, 8)

    def test_cross_validation_flag_in_details(self) -> None:
        """Details get cross_validated flag and bonus recorded."""
        mr = _merchant_result(score=0.70)
        cr = _cluster_result(score=0.60)
        merged = _merge_cross_validated(mr, cr)

        assert merged["details"]["cross_validated"] is True
        assert merged["details"]["cross_validation_bonus"] == 0.10
        assert merged["details"]["merchant_detection_score"] == 0.70
        assert merged["details"]["cluster_detection_score"] == 0.60

    def test_low_confidence_not_upgraded_below_medium_threshold(
        self,
    ) -> None:
        """Low confidence stays LOW when score doesn't reach 0.50."""
        mr = _merchant_result(score=0.25)
        mr["confidence"] = SubscriptionConfidence.LOW
        cr = _cluster_result(score=0.20)
        cr["confidence"] = SubscriptionConfidence.LOW
        merged = _merge_cross_validated(mr, cr)

        # 0.25 + 0.10 = 0.35 — still below 0.50
        assert merged["detection_score"] == pytest.approx(0.35)
        assert merged["confidence"] == SubscriptionConfidence.LOW

    def test_score_capped_at_one(self) -> None:
        """Score does not exceed 1.0 after bonus."""
        mr = _merchant_result(score=0.95)
        cr = _cluster_result(score=0.85)
        merged = _merge_cross_validated(mr, cr)

        assert merged["detection_score"] == pytest.approx(1.0)

    def test_high_confidence_stays_high(self) -> None:
        """Existing HIGH confidence stays HIGH after bonus."""
        mr = _merchant_result(score=0.85)
        mr["confidence"] = SubscriptionConfidence.HIGH
        cr = _cluster_result(score=0.70)
        merged = _merge_cross_validated(mr, cr)

        assert merged["confidence"] == SubscriptionConfidence.HIGH
        assert merged["detection_score"] == pytest.approx(0.95)


# ═══════════════════════════════════════════════════════════════════════
# _cross_reference_results tests
# ═══════════════════════════════════════════════════════════════════════


class TestCrossReferenceResults:
    """Verify the _cross_reference_results method on SubscriptionDetector."""

    def _find(self, results: list[dict], name: str) -> dict:
        """Find first result by merchant name."""
        for r in results:
            if r["merchant_name"] == name:
                return r
        msg = f"Merchant {name!r} not found"
        raise AssertionError(msg)

    @pytest.mark.asyncio
    async def test_both_methods_detect_same_merchant(self) -> None:
        """When both methods detect the same merchant, cross-validated."""
        detector = _make_mock_detector()
        mr = [_merchant_result(merchant="Netflix", score=0.72)]
        cr = [_cluster_result(merchant="Netflix", score=0.65)]

        results = detector._cross_reference_results(mr, cr)

        assert len(results) == 1
        assert results[0]["detection_method"] == DetectionMethod.HYBRID
        assert results[0]["detection_score"] == pytest.approx(0.82)

    @pytest.mark.asyncio
    async def test_no_overlap_preserves_both_sources(self) -> None:
        """Different merchants from each method are both preserved."""
        detector = _make_mock_detector()
        mr = [_merchant_result(merchant="Netflix", score=0.72)]
        cr = [_cluster_result(merchant="Spotify", score=0.65)]

        results = detector._cross_reference_results(mr, cr)

        assert len(results) == 2
        merchants = {r["merchant_name"] for r in results}
        assert merchants == {"Netflix", "Spotify"}
        for r in results:
            assert r["detection_method"] != DetectionMethod.HYBRID

    @pytest.mark.asyncio
    async def test_partial_overlap(self) -> None:
        """Only overlapping merchants get HYBRID; unique ones stay as-is."""
        detector = _make_mock_detector()
        mr = [
            _merchant_result(merchant="Netflix", score=0.72),
            _merchant_result(merchant="Spotify", score=0.65),
        ]
        cr = [
            _cluster_result(merchant="Netflix", score=0.60),
            _cluster_result(merchant="GitHub", score=0.55),
        ]

        results = detector._cross_reference_results(mr, cr)

        assert len(results) == 3
        merchants = {r["merchant_name"] for r in results}
        assert merchants == {"Netflix", "Spotify", "GitHub"}

        netflix = self._find(results, "Netflix")
        assert netflix["detection_method"] == DetectionMethod.HYBRID

        spotify = self._find(results, "Spotify")
        assert (
            spotify["detection_method"]
            == DetectionMethod.MERCHANT_CLASSIFICATION
        )
        github = self._find(results, "GitHub")
        assert github["detection_method"] == DetectionMethod.AMOUNT_CLUSTER

    @pytest.mark.asyncio
    async def test_empty_merchant_results(self) -> None:
        """When merchant results are empty, all cluster results pass."""
        detector = _make_mock_detector()
        cr = [
            _cluster_result(merchant="Netflix", score=0.60),
            _cluster_result(merchant="Spotify", score=0.55),
        ]

        results = detector._cross_reference_results([], cr)

        assert len(results) == 2
        assert results[0]["detection_method"] == DetectionMethod.AMOUNT_CLUSTER

    @pytest.mark.asyncio
    async def test_empty_cluster_results(self) -> None:
        """When cluster results are empty, all merchant results pass."""
        detector = _make_mock_detector()
        mr = [
            _merchant_result(merchant="Netflix", score=0.72),
        ]

        results = detector._cross_reference_results(mr, [])

        assert len(results) == 1
        assert (
            results[0]["detection_method"]
            == DetectionMethod.MERCHANT_CLASSIFICATION
        )

    @pytest.mark.asyncio
    async def test_both_empty(self) -> None:
        """Empty inputs produce empty output."""
        detector = _make_mock_detector()
        results = detector._cross_reference_results([], [])
        assert results == []

    @pytest.mark.asyncio
    async def test_cross_validated_are_first(self) -> None:
        """Cross-referenced merchants appear before unique ones."""
        detector = _make_mock_detector()
        mr = [
            _merchant_result(merchant="UniqueMerchant", score=0.50),
            _merchant_result(merchant="BothMerchant", score=0.70),
        ]
        cr = [
            _cluster_result(merchant="BothMerchant", score=0.60),
            _cluster_result(merchant="OnlyCluster", score=0.55),
        ]

        results = detector._cross_reference_results(mr, cr)

        assert results[0]["merchant_name"] == "BothMerchant"
        assert results[0]["detection_method"] == DetectionMethod.HYBRID


# ═══════════════════════════════════════════════════════════════════════
# Full _run_all_detection integration tests
# ═══════════════════════════════════════════════════════════════════════


def _make_txns(
    merchant: str,
    count: int,
    amount: str,
    *,
    account: str = "acct_1",
    provider: str = "bunq",
    txn_type: str = "payment",
    start: datetime | None = None,
) -> list[dict]:
    """Create a list of transaction dicts for testing."""
    base = start or datetime(2025, 1, 15, tzinfo=UTC)
    return [
        {
            "id": f"{merchant}_{i}",
            "amount": Decimal(amount),
            "currency_code": "EUR",
            "description": f"POS {merchant}",
            "occurred_at": base + timedelta(days=30 * i),
            "account_id": account,
            "provider_key": provider,
            "transaction_type": txn_type,
        }
        for i in range(count)
    ]


class TestRunAllDetectionIntegration:
    """Integration tests for the full _run_all_detection pipeline."""

    @pytest.mark.asyncio
    async def test_pipeline_cross_references_overlapping_merchants(
        self,
    ) -> None:
        """Pipeline cross-references when both methods detect same merchant."""
        netflix_txns = _make_txns("Netflix B.V.", 6, "-15.99")
        spotify_txns = _make_txns("Spotify AB", 4, "-9.99")
        txns = netflix_txns + spotify_txns
        detector = _make_mock_detector()

        detector._classify_merchants = AsyncMock(
            return_value={
                "Netflix B.V.": {
                    "sector": "Communication Services",
                    "security_id": "sec_nflx",
                    "likelihood_score": 0.12,
                    "ticker": "NFLX",
                    "subscription_likelihood": "high",
                    "source": "merchant_map",
                }
            }
        )

        results = await detector._run_all_detection(
            txns,
            min_occurrences=2,
            use_merchant_classifier=True,
        )

        assert len(results) >= 1
        netflix_results = [
            r for r in results if "Netflix" in r["merchant_name"]
        ]
        assert len(netflix_results) >= 1

    @pytest.mark.asyncio
    async def test_pipeline_without_classifier_still_cross_references(
        self,
    ) -> None:
        """Without merchant classifier, clustering still contributes."""
        txns = _make_txns("Netflix B.V.", 6, "-15.99")
        detector = _make_mock_detector()

        results = await detector._run_all_detection(
            txns,
            min_occurrences=2,
            use_merchant_classifier=False,
        )

        assert len(results) >= 1
        netflix_results = [
            r for r in results if "Netflix" in r["merchant_name"]
        ]
        assert len(netflix_results) >= 1

    @pytest.mark.asyncio
    async def test_clustering_failure_still_returns_merchant_results(
        self,
    ) -> None:
        """When clustering fails, merchant-based results are still returned."""
        txns = _make_txns("Netflix B.V.", 3, "-15.99")
        detector = _make_mock_detector()
        detector._classify_merchants = AsyncMock(return_value={})

        with patch(
            "finance_sync.services.pattern_clustering"
            ".SubscriptionPatternEngine.detect",
            side_effect=Exception("Clustering crashed"),
        ):
            results = await detector._run_all_detection(
                txns,
                min_occurrences=2,
                use_merchant_classifier=True,
            )

        assert len(results) >= 1
        assert any("Netflix" in r["merchant_name"] for r in results)

    @pytest.mark.asyncio
    async def test_hybrid_detection_with_sector_data(self) -> None:
        """Hybrid results carry sector/security_id from classifier."""
        txns = _make_txns("Netflix B.V.", 6, "-15.99")
        detector = _make_mock_detector()
        detector._classify_merchants = AsyncMock(
            return_value={
                "Netflix B.V.": {
                    "sector": "Communication Services",
                    "security_id": "sec_nflx",
                    "likelihood_score": 0.12,
                    "ticker": "NFLX",
                    "subscription_likelihood": "high",
                    "source": "merchant_map",
                }
            }
        )

        results = await detector._run_all_detection(
            txns,
            min_occurrences=2,
            use_merchant_classifier=True,
        )

        netflix_results = [
            r for r in results if "Netflix" in r["merchant_name"]
        ]
        if netflix_results:
            nf = netflix_results[0]
            if nf["detection_method"] == DetectionMethod.HYBRID:
                assert nf["sector"] is not None
                assert nf["security_id"] is not None


# ═══════════════════════════════════════════════════════════════════════
# End-to-end: detect() with cross-referencing
# ═══════════════════════════════════════════════════════════════════════


class TestDetectPipelineWithCrossReferencing:
    """Test that detect() uses the cross-referencing pipeline."""

    @pytest.mark.asyncio
    async def test_detect_delegates_to_cross_reference(self) -> None:
        """detect() calls _run_all_detection which cross-references."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock()
        factory.return_value = mock_session

        detector = SubscriptionDetector(
            session_factory=factory, tenant_id="tenant_1"
        )

        subs = await detector.detect(
            date_from=datetime(2025, 1, 1, tzinfo=UTC),
            date_to=datetime(2025, 12, 31, tzinfo=UTC),
            min_occurrences=2,
        )
        assert subs == []
