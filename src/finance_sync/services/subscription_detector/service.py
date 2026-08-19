"""Subscription detection service — integrates merchant classification
with pattern recognition for holistic subscription detection.

Combines:

* **Merchant classification** via GICS sector / fundamentals data
  (``classify_merchant``).
* **Pattern recognition** via amount consistency and interval regularity
  analysis (``PatternDetector``).
* **Cross-validation scoring** when both methods independently detect
  the same merchant — upgrades the result to HYBRID.
* **Edge-case handling** — overlapping subscriptions from the same
  merchant at different amounts, cancellation signals (refunds,
  cancellation keywords), and merchant-name normalisation.

Usage from an API layer::

    from finance_sync.services.subscription_detector.service import (
        SubscriptionDetectionService,
        Subscription,
    )

    svc = SubscriptionDetectionService(
        session_factory=container.session_factory,
    )
    subs: list[Subscription] = await svc.detect_subscriptions(
        user_id=auth.tenant_id,
    )

Usage with pre-fetched transactions (e.g. in tests)::

    svc = SubscriptionDetectionService()
    subs = await svc.detect_subscriptions(
        user_id="test",
        transactions=my_transactions,
    )
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)
from finance_sync.services.subscription_detector.detector import (  # type: ignore[reportPrivateUsage]
    _amounts_step_change_score,
    _classify_category,
    _compute_confidence_score,
    _detect_frequency_robust,
    _is_subscription_keyword,
    _normalise_merchant,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.services.subscription_detector.merchant_classifier import (
        MerchantClass,
    )

logger = structlog.get_logger(
    "finance_sync.services.subscription_detector.service"
)

# ── Constants ──────────────────────────────────────────────────────────

_DEFAULT_DAYS_BACK = 365
_MIN_OCCURRENCES = 2

# Regular expression for detecting cancellation signals in transaction
# descriptions.
_CANCELLATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        r"\bcancellation?\b",
        r"\bcancel\b",
        r"\bterminated?\b",
        r"\bended?\b",
        r"\bclosed?\b",
        r"\bdeactivated?\b",
        r"\bstopp?ed?\b",
        r"\brefund\b",
        r"\breversal\b",
        r"\bchargeback\b",
    ]
]

# Default configuration values — used when the caller does not supply
# explicit thresholds at construction.  These can be overridden via
# ``SubscriptionDetectionService(merchant_only_threshold=0.70, ...)``.
_DEFAULT_AMOUNT_BUCKET_TOLERANCE = Decimal("0.02")  # 2 %
_DEFAULT_MERCHANT_ONLY_THRESHOLD = 0.60
_DEFAULT_CROSS_VALIDATION_BONUS = 0.10


# ── Public data model ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Subscription:
    """A detected subscription — the result of running both merchant
    classification and pattern recognition on a user's transactions.

    Attributes:
        merchant_name: Normalised merchant / payee name.
        raw_description: Most recent raw transaction description.
        amount: Typical subscription amount (absolute value, positive).
        currency_code: ISO-4217 currency code.
        frequency_days: Expected interval in days (e.g. 30 for monthly),
            or ``None`` if no interval pattern was detected.
        frequency_label: Human-readable label (e.g. ``"monthly"``),
            or ``None``.
        confidence: High / Medium / Low confidence level.
        detection_score: Numeric detection score (0.0-1.0).
        detection_method: Which detection strategy produced this result.
        status: Subscription lifecycle status (``ACTIVE`` by default,
            ``CANCELLED`` when cancellation signals are found).
        transaction_ids: IDs of the transactions that matched.
        account_id: Primary account for this subscription.
        provider_key: Connector provider key.
        category: Subscription category (e.g. ``"streaming"``,
            ``"software"``), or ``None``.
        sector: GICS sector from merchant classification, or ``None``.
        security_id: DB security identifier if the merchant was
            resolved to a publicly traded company, or ``None``.
        fundamentals_available: Whether fundamentals data (PE, dividend
            yield) was used during classification.
        first_detected_at: Date of the earliest matched transaction.
        last_detected_at: Date of the most recent matched transaction.
        occurrence_count: How many transactions matched.
        details: Extra diagnostic context (amount consistency, intervals,
            keyword flags, sector boost, etc.).
    """

    merchant_name: str
    raw_description: str | None
    amount: Decimal
    currency_code: str
    frequency_days: int | None
    frequency_label: str | None
    confidence: SubscriptionConfidence
    detection_score: float
    detection_method: DetectionMethod
    status: SubscriptionStatus
    transaction_ids: list[str]
    account_id: str
    provider_key: str
    category: str | None = None
    sector: str | None = None
    security_id: str | None = None
    fundamentals_available: bool = False
    first_detected_at: datetime | None = None
    last_detected_at: datetime | None = None
    occurrence_count: int = 0
    details: dict[str, Any] = field(default_factory=dict[str, Any])


# ── Service ────────────────────────────────────────────────────────────


class SubscriptionDetectionService:
    """Integrate merchant classification with pattern recognition to
    produce a unified set of detected subscriptions.

    The service is designed to be usable in two modes:

    1. **DB-backed** — when constructed with a ``session_factory``, it
       fetches transactions from the database for the given ``user_id``.
    2. **Standalone** — when passed an explicit ``transactions`` list,
       no database dependency is required (useful for testing or when the
       caller already has the data in memory).

    Typical DB-backed usage::

        svc = SubscriptionDetectionService(
            session_factory=container.session_factory,
        )
        subs = await svc.detect_subscriptions(user_id=auth.tenant_id)

    Standalone usage::

        svc = SubscriptionDetectionService()
        subs = await svc.detect_subscriptions(
            user_id="test",
            transactions=my_txns,
        )
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        merchant_classifier: Any | None = None,
        min_occurrences: int = _MIN_OCCURRENCES,
        merchant_only_threshold: float = _DEFAULT_MERCHANT_ONLY_THRESHOLD,
        cross_validation_bonus: float = _DEFAULT_CROSS_VALIDATION_BONUS,
        amount_bucket_tolerance: Decimal | None = None,
    ) -> None:
        """Configure the detection service.

        Args:
            session_factory: Optional async DB session factory.  Required
                only when calling ``detect_subscriptions`` without an
                explicit ``transactions`` list.
            merchant_classifier: Optional ``MerchantClassifier`` instance
                for DB-backed fundamentals lookup.  When provided,
                merchants are enriched with fundamentals data (PE ratio,
                dividend yield) from the database.
            min_occurrences: Minimum number of occurrences required to
                consider a pattern (default 2).
            merchant_only_threshold: Minimum confidence for a merchant
                classified as a subscription provider to be included even
                when no recurring pattern is detected yet (default 0.60).
            cross_validation_bonus: Score increase applied when both
                merchant classification and pattern recognition
                independently confirm the same merchant (default 0.10).
            amount_bucket_tolerance: Fractional threshold for grouping
                amounts into the same bucket; amounts differing by less
                than this fraction are considered equal (default 0.02).
        """
        self._session_factory = session_factory
        self._merchant_classifier = merchant_classifier
        self._min_occurrences = min_occurrences
        self._merchant_only_threshold = merchant_only_threshold
        self._cross_validation_bonus = cross_validation_bonus
        self._amount_bucket_tolerance = (
            amount_bucket_tolerance
            if amount_bucket_tolerance is not None
            else _DEFAULT_AMOUNT_BUCKET_TOLERANCE
        )
        self._log = logger.bind()

    # ── Public API ───────────────────────────────────────────────────

    async def detect_subscriptions(
        self,
        user_id: str,
        *,
        transactions: list[dict[str, Any]] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_ids: Any | None = None,
    ) -> list[Subscription]:
        """Run subscription detection for a user.

        Args:
            user_id: User or tenant identifier.  Used to scope DB
                queries when ``transactions`` is not provided.
            transactions: Optional pre-fetched transaction list.  When
                omitted, transactions are fetched from the database using
                ``session_factory`` (must have been provided at
                construction).
            date_from: Earliest transaction date (default 365 days ago).
            date_to: Latest transaction date (default now).
            account_ids: Optional SQL predicate over ``Account.id`` (e.g.
                ``ReadScope.account_ids_subquery()``) restricting the
                analysis to visible accounts (account-scope scoping).  Only outgoing
                transactions on those accounts
                are considered.

        Returns:
            List of detected :class:`Subscription` objects, sorted by
            detection score descending (most reliable first).

        Raises:
            ValueError: If ``transactions`` is omitted and no
                ``session_factory`` was provided at construction.
        """
        if transactions is None:
            if self._session_factory is None:
                msg = (
                    "transactions list required when session_factory is None. "
                    "Either pass transactions explicitly or provide a "
                    "session_factory at construction."
                )
                raise ValueError(msg)
            transactions = await self._fetch_transactions(
                user_id,
                date_from=date_from,
                date_to=date_to,
                account_ids=account_ids,
            )

        if not transactions:
            self._log.info("no_transactions_to_analyze", user_id=user_id)
            return []

        self._log.info(
            "detection_start",
            user_id=user_id,
            transaction_count=len(transactions),
        )

        # ── Step 1: Run merchant classification ──────────────────────
        # Classify every unique merchant in the transaction set.
        classified_merchants: dict[str, Any] = {}
        cancellation_merchants: set[str] = set()
        try:
            (
                classified_merchants,
                cancellation_merchants,
            ) = await self._classify_all_merchants(transactions)
        except Exception:
            self._log.exception(
                "merchant_classification_failed",
                user_id=user_id,
            )
            # Continue with empty classifications — pattern detection
            # can still produce results.

        # ── Step 2: Run pattern detection ────────────────────────────
        # Group transactions by (merchant, amount bucket) to detect
        # distinct subscription patterns even from the same merchant.
        pattern_results: dict[str, list[dict[str, Any]]] = defaultdict(list)
        try:
            pattern_groups = self._group_by_merchant_and_amount(transactions)
            for key, group in pattern_groups.items():
                merchant, _amount = key
                if len(group) < self._min_occurrences:
                    continue

                analysis = self._analyze_group(
                    merchant,
                    group,
                    classification=classified_merchants.get(merchant),
                )
                if analysis is not None:
                    pattern_results[merchant].append(analysis)
        except Exception:
            self._log.exception(
                "pattern_detection_failed",
                user_id=user_id,
            )

        # ── Step 3: Build Subscription objects ──────────────────────
        subscriptions: list[Subscription] = []

        # 3a. Merchants with pattern-match results
        for merchant, results in pattern_results.items():
            for result in results:
                try:
                    subs = self._result_to_subscription(
                        result,
                        merchant,
                        cancelled=merchant in cancellation_merchants,
                    )
                    # Check whether merchant classification also flagged
                    # this merchant — apply cross-validation if so.
                    mc = classified_merchants.get(merchant)
                    if mc is not None and getattr(mc, "is_subscription", False):
                        subs = self._apply_cross_validation(subs, mc)

                    subscriptions.append(subs)
                except Exception:
                    self._log.exception(
                        "subscription_construction_failed",
                        merchant=merchant,
                    )

        # 3b. Merchants classified as subscription providers but without
        #     a detected pattern (e.g. only 1-2 transactions so far).
        try:
            classified_merchants_without_pattern = (
                self._find_classified_without_pattern(
                    classified_merchants,
                    pattern_results,
                    transactions,
                )
            )
            for (
                merchant,
                merchant_class,
            ) in classified_merchants_without_pattern:
                try:
                    txns_for_merchant = [
                        t
                        for t in transactions
                        if _normalise_merchant(t.get("description")) == merchant
                    ]
                    subs = self._merchant_only_subscription(
                        merchant,
                        merchant_class,
                        txns_for_merchant,
                        cancelled=merchant in cancellation_merchants,
                    )
                    subscriptions.append(subs)
                except Exception:
                    self._log.exception(
                        "merchant_only_subscription_failed",
                        merchant=merchant,
                    )
        except Exception:
            self._log.exception(
                "classified_without_pattern_failed",
                user_id=user_id,
            )

        # ── Step 4: Handle edge cases ────────────────────────────────

        # 4a. Resolve overlapping subscriptions (same merchant,
        #     different amounts that are within tolerance)
        try:
            subscriptions = self._resolve_overlaps(subscriptions)
        except Exception:
            self._log.exception("overlap_resolution_failed")

        # 4b. Final sort by detection score (most confident first)
        try:
            subscriptions.sort(key=lambda s: s.detection_score, reverse=True)
        except Exception:
            self._log.exception("subscription_sort_failed")

        self._log.info(
            "detection_complete",
            user_id=user_id,
            subscriptions_found=len(subscriptions),
        )

        return subscriptions

    # ── Merchant classification ─────────────────────────────────────

    async def _classify_all_merchants(
        self,
        transactions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], set[str]]:
        """Classify every unique merchant in the transaction set.

        When a ``MerchantClassifier`` was provided at construction, it
        is used for DB-backed classification that incorporates
        fundamentals data (PE ratio, dividend yield).  Otherwise the
        standalone ``classify_merchant`` function is used.

        Returns:
            Tuple of:
            - ``merchant_map``: ``{merchant_name: MerchantClass}``
            - ``cancellation_merchants``: set of merchant names that
              exhibit cancellation signals.
        """
        from finance_sync.services.subscription_detector.merchant_classifier import (
            classification_from_db,
            classify_merchant,
        )

        # Gather unique merchants
        merchants: set[str] = set()
        cancellation_merchants: set[str] = set()
        merchant_descriptions: dict[str, list[str]] = defaultdict(list)

        for txn in transactions:
            merchant = _normalise_merchant(txn.get("description"))
            merchants.add(merchant)
            desc = txn.get("description", "") or ""
            merchant_descriptions[merchant].append(desc)

            # Check for cancellation signals
            if desc and _has_cancellation_signal(desc):
                cancellation_merchants.add(merchant)

        # Classify each merchant
        classified: dict[str, Any] = {}

        if self._merchant_classifier is not None:
            # DB-backed classification with fundamentals enrichment
            for merchant in merchants:
                try:
                    raw_text = " ".join(merchant_descriptions[merchant])
                    category = (
                        _classify_category(raw_text) if raw_text else None
                    )
                    db_result = await self._merchant_classifier.classify(
                        merchant,
                        category=category,
                        use_fundamentals=True,
                    )
                    mc = classification_from_db(
                        _merchant_name=merchant,
                        sector=db_result.sector,
                        ticker=db_result.ticker,
                        subscription_likelihood=db_result.subscription_likelihood,
                        security_id=db_result.security_id,
                        source=db_result.source,
                    )
                    classified[merchant] = mc
                except Exception:
                    self._log.exception(
                        "merchant_classification_db_failed",
                        merchant=merchant,
                    )
                    # Fall back to standalone classification so a single
                    # DB failure doesn't block all merchants.
                    raw_text = " ".join(merchant_descriptions[merchant])
                    category = (
                        _classify_category(raw_text) if raw_text else None
                    )
                    mc = classify_merchant(merchant, category=category)
                    classified[merchant] = mc
        else:
            # Standalone classification (no DB fundamentals)
            for merchant in merchants:
                raw_text = " ".join(merchant_descriptions[merchant])
                category = _classify_category(raw_text) if raw_text else None
                mc = classify_merchant(merchant, category=category)
                classified[merchant] = mc

        classified_count = sum(
            1
            for mc in classified.values()
            if getattr(mc, "is_subscription", False)
        )
        self._log.info(
            "merchant_classification_done",
            total=len(classified),
            classified=classified_count,
            cancellation_merchants=len(cancellation_merchants),
        )

        return classified, cancellation_merchants

    # ── Pattern analysis ────────────────────────────────────────────

    @staticmethod
    def _group_by_merchant_and_amount(
        transactions: list[dict[str, Any]],
    ) -> dict[tuple[str, Decimal], list[dict[str, Any]]]:
        """Group transactions by normalised merchant *and* amount.

        Using both merchant name and amount as the grouping key lets us
        detect multiple distinct subscription plans from the same
        merchant (e.g. Basic €10 + Premium €20).

        Amounts are bucketed with a small tolerance so that minor price
        changes (e.g. €9.99 → €10.00) don't create spurious splits.
        """
        from decimal import ROUND_HALF_UP

        groups: dict[tuple[str, Decimal], list[dict[str, Any]]] = defaultdict(
            list
        )

        for txn in transactions:
            merchant = _normalise_merchant(txn.get("description"))
            raw_amount = txn.get("amount")
            if raw_amount is None:
                continue
            try:
                amount = Decimal(str(raw_amount))
            except (ValueError, TypeError):
                continue

            # Only outgoing (negative) transactions
            if amount >= 0:
                continue

            abs_amount = abs(amount)

            # Bucket the amount: round to nearest 0.01 base
            quantized = abs_amount.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            groups[(merchant, quantized)].append(txn)

        return groups

    @staticmethod
    def _outgoing_transactions(
        transactions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Filter to only outgoing (negative amount) transactions."""
        outgoing: list[dict[str, Any]] = []
        for txn in transactions:
            amount = txn.get("amount")
            if amount is not None:
                try:
                    if Decimal(str(amount)) < 0:
                        outgoing.append(txn)
                except (ValueError, TypeError):
                    pass
        return outgoing

    def _analyze_group(
        self,
        merchant: str,
        txns: list[dict[str, Any]],
        *,
        classification: Any = None,
    ) -> dict[str, Any] | None:
        """Analyse a single (merchant, amount) group for subscription
        patterns.

        Returns a result dict (same shape as ``PatternResult`` fields)
        or ``None`` if the group does not form a credible pattern.
        """
        txns_sorted = sorted(txns, key=lambda t: t["occurred_at"])
        amounts = [Decimal(str(t["amount"])) for t in txns_sorted]
        dates = [t["occurred_at"] for t in txns_sorted]
        descriptions = [t.get("description", "") or "" for t in txns_sorted]

        # ── Extract classification data ──────────────────────────────
        sector: str | None = None
        sector_boost: float = 0.0
        security_id: str | None = None
        fundamentals_available: bool = False

        if classification is not None:
            if hasattr(classification, "sector"):
                sector = classification.sector
                sector_boost = (
                    getattr(classification, "likelihood_score", 0.0) or 0.0
                )
                security_id = getattr(classification, "security_id", None)
                fundamentals_available = getattr(
                    classification, "fundamentals_available", False
                )
            elif isinstance(classification, dict):
                classification = cast("dict[str, Any]", classification)
                sector = classification.get("sector")
                sector_boost = (
                    classification.get("likelihood_score", 0.0) or 0.0
                )
                security_id = classification.get("security_id")
                fundamentals_available = classification.get(
                    "fundamentals_available", False
                )

        # ── Amount consistency ───────────────────────────────────────
        from finance_sync.services.subscription_detector.detector import (  # type: ignore[reportPrivateUsage]
            _amounts_are_consistent,
        )

        amount_consistency = _amounts_are_consistent(amounts)

        # Detect step-change pattern (price changes over time)
        if amount_consistency == 0.0:
            step_score = _amounts_step_change_score([abs(a) for a in amounts])
            if step_score > 0.0:
                amount_consistency = step_score

        if amount_consistency == 0.0:
            return None

        # ── Intervals ────────────────────────────────────────────────
        intervals_days: list[float] = []
        for i in range(1, len(dates)):
            if dates[i] and dates[i - 1]:
                diff = (dates[i] - dates[i - 1]).total_seconds() / 86400.0
                if diff > 0:
                    intervals_days.append(diff)

        frequency_days, frequency_label = _detect_frequency_robust(
            intervals_days
        )

        # Interval regularity (coefficient of variation)
        interval_regularity = 0.0
        if intervals_days:
            mean_interval = sum(intervals_days) / len(intervals_days)
            if mean_interval > 0 and len(intervals_days) > 1:
                variance = sum((d - mean_interval) ** 2 for d in intervals_days)
                std_dev = (variance / (len(intervals_days) - 1)) ** 0.5
                cv = std_dev / mean_interval
                if cv <= 0.1:
                    interval_regularity = 1.0
                elif cv <= 0.25:
                    interval_regularity = 0.7
                elif cv <= 0.5:
                    interval_regularity = 0.4
                else:
                    interval_regularity = 0.1

        # ── Keyword / category heuristics ────────────────────────────
        raw_text = " ".join(descriptions)
        has_keyword = any(
            _is_subscription_keyword(d) for d in descriptions
        ) or _is_subscription_keyword(raw_text)
        category = _classify_category(raw_text)

        # ── Confidence ───────────────────────────────────────────────
        confidence, score = _compute_confidence_score(
            occurrence_count=len(txns),
            amount_consistency=amount_consistency,
            interval_regularity=interval_regularity,
            has_keyword=has_keyword,
            has_category=category is not None,
            sector_boost=sector_boost,
        )

        # ── Detection method ─────────────────────────────────────────
        if sector is not None:
            method = DetectionMethod.MERCHANT_CLASSIFICATION
        elif amount_consistency >= 1.0 and frequency_label is not None:
            method = DetectionMethod.EXACT_AMOUNT
        elif amount_consistency > 0.0 and frequency_label is not None:
            method = DetectionMethod.SIMILAR_AMOUNT
        elif interval_regularity > 0.5:
            method = DetectionMethod.REGULAR_INTERVAL
        else:
            method = DetectionMethod.EXACT_AMOUNT

        # ── Build result dict ────────────────────────────────────────
        latest = txns_sorted[-1]
        return {
            "merchant_name": merchant,
            "raw_description": descriptions[-1] if descriptions else None,
            "amount": abs(amounts[0]),
            "currency_code": latest.get("currency_code", "EUR"),
            "frequency_days": frequency_days,
            "frequency_label": frequency_label,
            "confidence": confidence,
            "detection_score": score,
            "detection_method": method,
            "transaction_ids": [t["id"] for t in txns_sorted],
            "account_id": latest["account_id"],
            "provider_key": latest["provider_key"],
            "category": category,
            "sector": sector,
            "security_id": security_id,
            "fundamentals_available": fundamentals_available,
            "first_detected_at": dates[0] if dates else None,
            "last_detected_at": dates[-1] if dates else None,
            "occurrence_count": len(txns),
            "details": {
                "amount_consistency": amount_consistency,
                "interval_regularity": interval_regularity,
                "intervals_days": [round(i, 1) for i in intervals_days],
                "has_keyword": has_keyword,
                "amounts": [str(a) for a in amounts],
                "sector_boost": sector_boost,
            },
        }

    # ── Edge cases ──────────────────────────────────────────────────

    @staticmethod
    def _has_cancellation_signal(description: str) -> bool:
        """Check if a transaction description contains a cancellation
        or refund signal."""
        for pattern in _CANCELLATION_PATTERNS:
            if pattern.search(description):
                return True
        return False

    def _find_classified_without_pattern(
        self,
        classified_merchants: dict[str, Any],
        pattern_results: dict[str, list[dict[str, Any]]],
        transactions: list[dict[str, Any]],
    ) -> list[tuple[str, Any]]:
        """Find merchants that are classified as subscription providers
        but were *not* detected by pattern recognition.

        These are included as lower-confidence candidates — useful when
        a user has just started a subscription and hasn't accumulated
        enough occurrences for pattern detection yet.
        """
        candidates: list[tuple[str, Any]] = []

        for merchant, mc in classified_merchants.items():
            if not getattr(mc, "is_subscription", False):
                continue
            if merchant in pattern_results:
                continue

            # Only include if there's at least one outgoing transaction
            # from this merchant
            has_outgoing = any(
                _normalise_merchant(t.get("description")) == merchant
                and Decimal(str(t.get("amount", 0))) < 0
                for t in transactions
            )
            if not has_outgoing:
                continue

            # Only include if the merchant classification confidence
            # meets the configured threshold
            mc_confidence = (
                getattr(mc, "confidence", 0.0)
                if hasattr(mc, "confidence")
                else 0.0
            )
            if mc_confidence >= self._merchant_only_threshold:
                candidates.append((merchant, mc))

        return candidates

    @staticmethod
    def _result_to_subscription(
        result: dict[str, Any],
        merchant: str,
        *,
        cancelled: bool = False,
    ) -> Subscription:
        """Convert a pattern-analysis result dict to a ``Subscription``
        instance."""
        return Subscription(
            merchant_name=merchant,
            raw_description=result.get("raw_description"),
            amount=result.get("amount", Decimal(0)),
            currency_code=result.get("currency_code", "EUR"),
            frequency_days=result.get("frequency_days"),
            frequency_label=result.get("frequency_label"),
            confidence=result.get("confidence", SubscriptionConfidence.LOW),
            detection_score=result.get("detection_score", 0.0),
            detection_method=result.get(
                "detection_method", DetectionMethod.EXACT_AMOUNT
            ),
            status=SubscriptionStatus.CANCELLED
            if cancelled
            else SubscriptionStatus.ACTIVE,
            transaction_ids=result.get("transaction_ids", []),
            account_id=result.get("account_id", ""),
            provider_key=result.get("provider_key", ""),
            category=result.get("category"),
            sector=result.get("sector"),
            security_id=result.get("security_id"),
            fundamentals_available=result.get("fundamentals_available", False),
            first_detected_at=result.get("first_detected_at"),
            last_detected_at=result.get("last_detected_at"),
            occurrence_count=result.get("occurrence_count", 0),
            details=result.get("details", {}),
        )

    def _merchant_only_subscription(
        self,
        merchant: str,
        merchant_class: MerchantClass,
        transactions: list[dict[str, Any]],
        *,
        cancelled: bool = False,
    ) -> Subscription:
        """Build a ``Subscription`` from merchant classification alone
        (no pattern data available).

        Used when a merchant is classified as a subscription provider
        but hasn't yet produced enough transactions for pattern
        detection.
        """
        # Sort to get the most recent transaction
        txns_sorted = sorted(transactions, key=lambda t: t["occurred_at"])
        latest = txns_sorted[-1] if txns_sorted else {}
        amounts = [
            abs(Decimal(str(t["amount"])))
            for t in txns_sorted
            if t.get("amount") is not None
        ]
        avg_amount = sum(amounts) / len(amounts) if amounts else Decimal(0)

        descriptions = [t.get("description", "") or "" for t in txns_sorted]
        raw_text = " ".join(descriptions) if descriptions else ""
        category = _classify_category(raw_text) if raw_text else None

        mc_confidence = getattr(merchant_class, "confidence", 0.0) or 0.0
        likelihood_score = (
            getattr(merchant_class, "likelihood_score", 0.0) or 0.0
        )

        # Compute detection score (merchant-based only)
        score = mc_confidence + likelihood_score
        score = min(score, 1.0)

        if score >= 0.80:
            confidence = SubscriptionConfidence.HIGH
        elif score >= 0.50:
            confidence = SubscriptionConfidence.MEDIUM
        else:
            confidence = SubscriptionConfidence.LOW

        return Subscription(
            merchant_name=merchant,
            raw_description=latest.get("description"),
            amount=Decimal(avg_amount),
            currency_code=latest.get("currency_code", "EUR"),
            frequency_days=None,
            frequency_label=None,
            confidence=confidence,
            detection_score=round(score, 4),
            detection_method=DetectionMethod.MERCHANT_CLASSIFICATION,
            status=SubscriptionStatus.CANCELLED
            if cancelled
            else SubscriptionStatus.ACTIVE,
            transaction_ids=[t["id"] for t in txns_sorted if t.get("id")],
            account_id=str(latest.get("account_id", ""))
            if latest.get("account_id")
            else "",
            provider_key=latest.get("provider_key", ""),
            category=category,
            sector=getattr(merchant_class, "sector", None),
            security_id=getattr(merchant_class, "security_id", None),
            fundamentals_available=getattr(
                merchant_class, "fundamentals_available", False
            ),
            first_detected_at=txns_sorted[0].get("occurred_at")
            if txns_sorted
            else None,
            last_detected_at=latest.get("occurred_at"),
            occurrence_count=len(txns_sorted),
            details={
                "merchant_confidence": mc_confidence,
                "merchant_likelihood": getattr(
                    merchant_class, "subscription_likelihood", None
                ),
                "merchant_source": getattr(merchant_class, "source", None),
                "merchant_ticker": getattr(merchant_class, "ticker", None),
                "category": category,
                "amounts": [str(a) for a in amounts],
                "detection_note": "merchant_classification_only",
            },
        )

    def _apply_cross_validation(
        self,
        subscription: Subscription,
        merchant_class: MerchantClass,
    ) -> Subscription:
        """Apply the cross-validation bonus when both merchant
        classification and pattern recognition independently detected
        the same merchant.

        Upgrades the detection method to HYBRID, adds confidence bonus,
        and enriches with sector data from the classifier.
        """
        if not merchant_class.is_subscription:
            return subscription

        # Don't upgrade if already HYBRID
        if subscription.detection_method == DetectionMethod.HYBRID:
            return subscription

        # Apply cross-validation bonus
        new_score = min(
            1.0,
            round(
                subscription.detection_score + self._cross_validation_bonus, 4
            ),
        )

        # Determine confidence level from new score
        if new_score >= 0.80:
            new_confidence = SubscriptionConfidence.HIGH
        elif new_score >= 0.50:
            new_confidence = SubscriptionConfidence.MEDIUM
        else:
            new_confidence = subscription.confidence

        # Merge sector data if missing
        new_sector = subscription.sector or getattr(
            merchant_class, "sector", None
        )

        # Merge fundamentals data
        new_security_id = subscription.security_id or getattr(
            merchant_class, "security_id", None
        )
        new_fundamentals_available = (
            subscription.fundamentals_available
            or getattr(merchant_class, "fundamentals_available", False)
        )

        # Build updated details
        details = dict(subscription.details)
        details["cross_validated"] = True
        details["cross_validation_bonus"] = self._cross_validation_bonus
        details["merchant_detection_score"] = getattr(
            merchant_class, "confidence", 0.0
        )

        return Subscription(
            merchant_name=subscription.merchant_name,
            raw_description=subscription.raw_description,
            amount=subscription.amount,
            currency_code=subscription.currency_code,
            frequency_days=subscription.frequency_days,
            frequency_label=subscription.frequency_label,
            confidence=new_confidence,
            detection_score=new_score,
            detection_method=DetectionMethod.HYBRID,
            status=subscription.status,
            transaction_ids=subscription.transaction_ids,
            account_id=subscription.account_id,
            provider_key=subscription.provider_key,
            category=subscription.category,
            sector=new_sector,
            security_id=new_security_id,
            fundamentals_available=new_fundamentals_available,
            first_detected_at=subscription.first_detected_at,
            last_detected_at=subscription.last_detected_at,
            occurrence_count=subscription.occurrence_count,
            details=details,
        )

    def _resolve_overlaps(
        self,
        subscriptions: list[Subscription],
    ) -> list[Subscription]:
        """Resolve overlapping subscriptions from the same merchant.

        When multiple subscriptions share the same merchant name and
        their amounts are within the tolerance threshold, keep only
        the highest-scoring entry.

        However, when a merchant genuinely has multiple subscriptions
        at *different* amounts (e.g. Basic €10 + Premium €20), both
        are preserved.
        """
        if not subscriptions:
            return []

        # Group by merchant name
        by_merchant: dict[str, list[Subscription]] = defaultdict(list)
        for sub in subscriptions:
            by_merchant[sub.merchant_name].append(sub)

        resolved: list[Subscription] = []
        for merchant, subs in by_merchant.items():
            if len(subs) == 1:
                resolved.append(subs[0])
                continue

            # Multiple subscriptions for the same merchant — check
            # whether they're at truly distinct amounts or duplicates.
            # Two amounts are "the same" if they differ by less than
            # the tolerance fraction.
            distinct: list[Subscription] = []
            for sub in sorted(
                subs, key=lambda s: s.detection_score, reverse=True
            ):
                # Check if we already have a similar amount
                is_duplicate = False
                for existing in distinct:
                    if existing.amount == Decimal(0):
                        continue
                    if sub.amount == Decimal(0):
                        continue
                    diff = abs(sub.amount - existing.amount)
                    max_amt = max(sub.amount, existing.amount)
                    if (
                        max_amt > 0
                        and (diff / max_amt) <= self._amount_bucket_tolerance
                    ):
                        is_duplicate = True
                        break

                if not is_duplicate:
                    distinct.append(sub)

            resolved.extend(distinct)

            if len(distinct) < len(subs):
                logger.debug(
                    "merged_overlapping_subscriptions",
                    merchant=merchant,
                    before=len(subs),
                    after=len(distinct),
                )

        return resolved

    # ── Configuration introspection ───────────────────────────────

    @property
    def config(self) -> dict[str, object]:
        """Return the current detection thresholds as a dict.

        Useful for logging, debugging, or serialising the configuration
        for reproducibility across sessions.
        """
        return {
            "min_occurrences": self._min_occurrences,
            "merchant_only_threshold": self._merchant_only_threshold,
            "cross_validation_bonus": self._cross_validation_bonus,
            "amount_bucket_tolerance": str(self._amount_bucket_tolerance),
        }

    # ── DB-backed transaction fetching ────────────────────────────

    async def _fetch_transactions(
        self,
        user_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        account_ids: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch outgoing transactions from the database for a user.

        Requires ``session_factory`` to have been provided at
        construction.  When ``account_ids`` is given (a SQL predicate
        such as ``scope.account_ids_subquery()``), only transactions on
        those accounts are returned (account-scope scoping).
        """
        if date_from is None:
            date_from = datetime.now(UTC) - timedelta(days=_DEFAULT_DAYS_BACK)
        if date_to is None:
            date_to = datetime.now(UTC)

        if self._session_factory is None:
            msg = (
                "session_factory required for DB-backed transaction fetching. "
                "Call detect_subscriptions with an explicit transactions list "
                "or provide a session_factory at construction."
            )
            raise RuntimeError(msg)
        from sqlalchemy import select

        from finance_sync.models.transaction import Transaction

        async with self._session_factory() as session:  # type: ignore[union-attr]
            stmt = (
                select(
                    Transaction.id,
                    Transaction.amount,
                    Transaction.currency_code,
                    Transaction.description,
                    Transaction.occurred_at,
                    Transaction.account_id,
                    Transaction.provider_key,
                    Transaction.transaction_type,
                )
                .where(
                    Transaction.tenant_id == user_id,  # type: ignore[attr-defined]
                    Transaction.amount < 0,  # type: ignore[attr-defined]
                    Transaction.occurred_at >= date_from,  # type: ignore[attr-defined]
                    Transaction.occurred_at <= date_to,  # type: ignore[attr-defined]
                    Transaction.status.in_(  # type: ignore[attr-defined]
                        [
                            "booked",
                            "pending",
                        ]
                    ),
                )
                .order_by(Transaction.occurred_at.asc())  # type: ignore[attr-defined]
            )
            if account_ids is not None:
                stmt = stmt.where(
                    Transaction.account_id.in_(  # type: ignore[attr-defined]
                        account_ids
                    )
                )

            result = await session.execute(stmt)
            rows = result.all()

            return [
                {
                    "id": str(row.id),
                    "amount": row.amount,
                    "currency_code": row.currency_code,
                    "description": row.description or "",
                    "occurred_at": row.occurred_at,
                    "account_id": str(row.account_id),
                    "provider_key": row.provider_key,
                    "transaction_type": row.transaction_type,
                }
                for row in rows
            ]


# ── Free helpers ────────────────────────────────────────────────────


def _has_cancellation_signal(description: str) -> bool:
    """Check if a transaction description contains a cancellation or
    refund signal.

    Public entry point for external callers that want to check
    individual descriptions without instantiating the service.
    """
    for pattern in _CANCELLATION_PATTERNS:
        if pattern.search(description):
            return True
    return False


__all__ = [
    "Subscription",
    "SubscriptionDetectionService",
    "_has_cancellation_signal",
]
