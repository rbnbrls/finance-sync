"""Subscription detection package — identifies recurring transactions.

Consists of:
- detector: The original module with SubscriptionDetector service and
  pure-function helpers (normalisation, classification, confidence).
- merchant_classifier: Standalone merchant classification using
  fundamentals data and the known-merchant ticker map.
- pattern_detector: Standalone pattern recognition for recurring
  transactions (amount consistency, interval regularity, frequency).
"""

from __future__ import annotations

from finance_sync.services.subscription_detector.detector import (
    # ── Regex patterns ─────────────────────────────────────────────────
    _CATEGORY_KEYWORDS,
    # ── Constants ──────────────────────────────────────────────────────
    _DEFAULT_DAYS_BACK,
    _FREQUENCY_BANDS,
    _MAX_AMOUNT_ABSOLUTE,
    _MAX_AMOUNT_VARIANCE_PCT,
    _MIN_OCCURRENCES,
    _SUBSCRIPTION_KEYWORDS,
    # ── Service ────────────────────────────────────────────────────────
    SubscriptionDetector,
    # ─── Pure functions ────────────────────────────────────────────────
    _amounts_are_consistent,
    _amounts_step_change_score,
    _classify_category,
    _compute_confidence_score,
    _detect_frequency,
    _detect_frequency_robust,
    _is_subscription_keyword,
    _merge_cross_validated,
    _normalise_merchant,
)

# ── Re-export standalone merchant classifier so callers can use:
#   from finance_sync.services.subscription_detector import classify_merchant
from finance_sync.services.subscription_detector.merchant_classifier import (
    MerchantClass,
    classification_from_db,
    classify_merchant,
    normalise_merchant_name,
    resolve_ticker,
    sector_subscription_likelihood,
)

# Re-export standalone pattern detector
from finance_sync.services.subscription_detector.pattern_detector import (
    PatternDetector,
    PatternResult,
)

# ── Re-export integrated service
from finance_sync.services.subscription_detector.service import (
    Subscription,
    SubscriptionDetectionService,
    _has_cancellation_signal,
)

__all__ = [  # noqa: RUF022
    "_CATEGORY_KEYWORDS",
    "_DEFAULT_DAYS_BACK",
    "_FREQUENCY_BANDS",
    "_MAX_AMOUNT_ABSOLUTE",
    "_MAX_AMOUNT_VARIANCE_PCT",
    "_MIN_OCCURRENCES",
    "_SUBSCRIPTION_KEYWORDS",
    # ── Merchant classification (standalone) ──────────────────────────
    "MerchantClass",
    "classification_from_db",
    # ── Pattern detector (standalone) ───────────────────────────────────
    "PatternDetector",
    "PatternResult",
    # ── Integrated service ───────────────────────────────────────────
    "Subscription",
    "SubscriptionDetectionService",
    # ── Helpers ─────────────────────────────────────────────────────────
    "_has_cancellation_signal",
    # ── Service ────────────────────────────────────────────────────────
    "SubscriptionDetector",
    # ── Functions (detector) ───────────────────────────────────────────
    "_amounts_are_consistent",
    "_amounts_step_change_score",
    "_classify_category",
    "_compute_confidence_score",
    "_detect_frequency",
    "_detect_frequency_robust",
    "_is_subscription_keyword",
    "_merge_cross_validated",
    "_normalise_merchant",
    "classify_merchant",
    "normalise_merchant_name",
    "resolve_ticker",
    "sector_subscription_likelihood",
]
