"""Standalone merchant classification using fundamentals/securities data.

Provides a function-oriented API — no service class needed — for
classifying a merchant's subscription likelihood based on its
GICS sector, ticker, and optional fundamental metrics.

Can be used independently of the full ``SubscriptionDetector`` pipeline.

Usage::

    from finance_sync.services.subscription_detector import (
        classify_merchant,
        MerchantClass,
    )

    result = classify_merchant("Netflix B.V.")
    print(result.is_subscription, result.confidence)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from finance_sync.services.merchant_classifier import (  # type: ignore[reportPrivateUsage]
    _LIKELIHOOD_BOOST,
    LIKELIHOOD_HIGH,
    LIKELIHOOD_LOW,
    LIKELIHOOD_MEDIUM,
    _adjust_likelihood_with_fundamentals,
    _get_sector_likelihood,
    _normalise_merchant_name,
    _resolve_merchant_entry,
    _sector_from_category,
)

if TYPE_CHECKING:
    from decimal import Decimal

# ── Confidence mapping ──────────────────────────────────────────────────

# Maps subscription-likelihood labels to a 0-1 confidence score.
_LIKELIHOOD_TO_CONFIDENCE: dict[str, float] = {
    LIKELIHOOD_HIGH: 0.85,
    LIKELIHOOD_MEDIUM: 0.60,
    LIKELIHOOD_LOW: 0.30,
}

_THRESHOLD_SUBSCRIPTION = 0.50  # confidence ≥ this → is_subscription = True


# ── DTO ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MerchantClass:
    """Classification result for a single merchant.

    Attributes:
        is_subscription: Whether the merchant is likely a subscription
            provider (confidence >= 0.50).
        confidence: Numeric confidence in the classification (0.0-1.0).
        sector: GICS sector name if resolved, else ``None``.
        ticker: Stock ticker symbol if the merchant is publicly traded,
            else ``None``.
        source: How the classification was derived
            (``"merchant_map"``, ``"category_map"``, or ``"sector_map"``).
        subscription_likelihood: The raw likelihood label
            (``"high"``, ``"medium"``, ``"low"``).
        likelihood_score: The numeric boost that would be applied
            to a detection confidence score (0.0-0.12).
        fundamentals_available: Whether fundamentals data (PE ratio,
            dividend yield, etc.) was used to adjust the classification.
        security_id: DB identifier for the resolved security (if the
            merchant is a publicly traded company), else ``None``.
    """

    is_subscription: bool
    confidence: float
    sector: str | None = None
    ticker: str | None = None
    source: str = "sector_map"
    subscription_likelihood: str = LIKELIHOOD_MEDIUM
    likelihood_score: float = 0.0
    fundamentals_available: bool = False
    security_id: str | None = None


# ── Standalone function ─────────────────────────────────────────────────


def classify_merchant(
    merchant_name: str,
    category: str | None = None,
    *,
    pe_ratio: Decimal | None = None,
    dividend_yield: Decimal | None = None,
    security_id: str | None = None,
    fundamentals_available: bool = False,
) -> MerchantClass:
    """Classify a merchant for subscription likelihood.

    Resolution priority:
    1. **Merchant → ticker map** — known subscription merchants
       (e.g. Netflix, Spotify, GitHub) are resolved to their GICS
       sector and ticker symbol.
    2. **Category-based sector mapping** — when the merchant was
       already categorised (e.g. "streaming", "software") by keyword
       detection, the category is mapped to a GICS sector.
    3. **Sector-based default** — falls back to a medium-likelihood
       default when neither the map nor a category applies.

    Args:
        merchant_name: Raw or normalised merchant name.
        category: Optional subscription category label from
            keyword-based detection (e.g. ``"streaming"``).
        pe_ratio: Price-to-Earnings ratio for fundamentals-aware
            adjustment (optional).
        dividend_yield: Dividend yield (e.g. 0.035 = 3.5 %) for
            fundamentals-aware adjustment (optional).
        security_id: DB identifier for the resolved security, used
            when the merchant was resolved via DB-backed classification.
        fundamentals_available: Whether fundamentals data was used
            to adjust the classification (default ``False``).

    Returns:
        A :class:`MerchantClass` with subscription assessment.
    """
    # ── Step 1 — Try merchant ticker map ──────────────────────────────
    entry: dict[str, Any] | None = _resolve_merchant_entry(merchant_name)
    if entry is not None:
        sector = entry["sector"]
        ticker = entry.get("ticker")
        likelihood = _get_sector_likelihood(sector)

        # Fundamentals-aware adjustment
        adjusted = _adjust_likelihood_with_fundamentals(
            likelihood,
            pe_ratio=pe_ratio,
            dividend_yield=dividend_yield,
        )

        confidence = _likelihood_to_confidence(adjusted)
        return MerchantClass(
            is_subscription=confidence >= _THRESHOLD_SUBSCRIPTION,
            confidence=confidence,
            sector=sector,
            ticker=ticker,
            source="merchant_map",
            subscription_likelihood=adjusted,
            likelihood_score=_LIKELIHOOD_BOOST.get(adjusted, 0.0),
            fundamentals_available=fundamentals_available,
            security_id=security_id,
        )

    # ── Step 2 — Category-based sector mapping ────────────────────────
    sector = _sector_from_category(category)
    if sector is not None:
        likelihood = _get_sector_likelihood(sector)
        confidence = _likelihood_to_confidence(likelihood)
        return MerchantClass(
            is_subscription=confidence >= _THRESHOLD_SUBSCRIPTION,
            confidence=confidence,
            sector=sector,
            source="category_map",
            subscription_likelihood=likelihood,
            likelihood_score=_LIKELIHOOD_BOOST.get(likelihood, 0.0),
            fundamentals_available=fundamentals_available,
            security_id=security_id,
        )

    # ── Step 3 — Default ─────────────────────────────────────────────
    return MerchantClass(
        is_subscription=True,
        confidence=_LIKELIHOOD_TO_CONFIDENCE[LIKELIHOOD_MEDIUM],
        source="sector_map",
        subscription_likelihood=LIKELIHOOD_MEDIUM,
        likelihood_score=_LIKELIHOOD_BOOST[LIKELIHOOD_MEDIUM],
        fundamentals_available=fundamentals_available,
        security_id=security_id,
    )


# ── Internal helpers ────────────────────────────────────────────────────


def _likelihood_to_confidence(likelihood: str) -> float:
    """Map a subscription-likelihood label to a numeric confidence."""
    return _LIKELIHOOD_TO_CONFIDENCE.get(likelihood, 0.30)


def normalise_merchant_name(merchant_name: str) -> str:
    """Normalise a merchant name for lookup in the ticker map.

    Convenience alias for the shared normalisation function.
    """
    return _normalise_merchant_name(merchant_name)


def resolve_ticker(merchant_name: str) -> str | None:
    """Resolve a merchant name to a ticker symbol.

    Returns ``None`` for private companies and unrecognised merchants.
    """
    entry = _resolve_merchant_entry(merchant_name)
    return entry.get("ticker") if entry else None


def sector_subscription_likelihood(sector: str) -> str:
    "Return the default subscription likelihood for a GICS sector."
    return _get_sector_likelihood(sector)


def classification_from_db(
    _merchant_name: str,
    sector: str | None = None,
    ticker: str | None = None,
    subscription_likelihood: str = LIKELIHOOD_MEDIUM,
    security_id: str | None = None,
    source: str = "sector_map",
) -> MerchantClass:
    """Convert a DB-backed ``MerchantClassification`` into a ``MerchantClass``.

    This bridges the gap between ``MerchantClassifier`` (which uses DB
    fundamentals) and the subscription-detection pipeline (which uses
    the standalone ``MerchantClass`` DTO).

    Args:
        _merchant_name: Normalised merchant name (used only for logging).
        sector: GICS sector name.
        ticker: Stock ticker symbol, if known.
        subscription_likelihood: ``"high"``, ``"medium"``, or ``"low"``.
        security_id: DB security identifier, if resolved.
        source: Classification source label.

    Returns:
        A :class:`MerchantClass` compatible with the detection pipeline.
    """
    likelihood = subscription_likelihood or LIKELIHOOD_MEDIUM
    confidence = _likelihood_to_confidence(likelihood)
    return MerchantClass(
        is_subscription=confidence >= _THRESHOLD_SUBSCRIPTION,
        confidence=confidence,
        sector=sector,
        ticker=ticker,
        source=source or "sector_map",
        subscription_likelihood=likelihood,
        likelihood_score=_LIKELIHOOD_BOOST.get(likelihood, 0.0),
        fundamentals_available=security_id is not None,
        security_id=security_id,
    )


__all__ = [
    "MerchantClass",
    "classification_from_db",
    "classify_merchant",
    "normalise_merchant_name",
    "resolve_ticker",
    "sector_subscription_likelihood",
]
