"""Standalone pattern recognition for recurring transactions.

Detects subscription-like patterns in transaction history by analysing
amount consistency, interval regularity, and merchant repetition.  Fully
independent of any database — operates purely on transaction dicts.

Can be used standalone or combined with merchant classification via the
``classifications`` parameter for cross-validation.

Typical usage::

    from finance_sync.services.subscription_detector.pattern_detector import (
        PatternDetector,
        PatternResult,
    )

    detector = PatternDetector(
        min_occurrences=3,
        max_amount_variance_pct=Decimal("0.10"),
    )
    results: list[PatternResult] = detector.detect(transactions)

    for r in results:
        print(r.merchant_name, r.confidence, r.frequency_label)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
)
from finance_sync.services.subscription_detector.detector import (
    _amounts_are_consistent,
    _amounts_step_change_score,
    _classify_category,
    _compute_confidence_score,
    _detect_frequency_robust,
    _is_subscription_keyword,
    _normalise_merchant,
)

if TYPE_CHECKING:
    from datetime import datetime

# ── Defaults (tunable) ──────────────────────────────────────────────────

_DEFAULT_MIN_OCCURRENCES = 2
_DEFAULT_MAX_AMOUNT_VARIANCE_PCT = Decimal("0.05")  # 5 %
_DEFAULT_MAX_AMOUNT_ABSOLUTE = Decimal("2.00")  # EUR 2 for small amounts
_DEFAULT_ALLOW_ZERO_AMOUNT = False

_MSG_TRANSACTIONS_NONE = "transactions must not be None"


# ── Result type ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PatternResult:
    """Result of pattern detection for a single merchant group.

    Attributes:
        merchant_name: Normalised merchant / payee name.
        raw_description: Most recent raw description text.
        amount: Representative transaction amount (absolute value).
        currency_code: ISO-4217 currency code.
        frequency_days: Expected interval in days (e.g. 30 for monthly),
            or ``None`` when no frequency pattern was found.
        frequency_label: Human-readable label (e.g. ``"monthly"``),
            or ``None``.
        confidence: High / Medium / Low confidence level.
        detection_score: Numeric score 0.0-1.0.
        detection_method: Which method detected the pattern.
        transaction_ids: IDs of the matched transactions.
        account_id: Account ID of the most recent transaction.
        provider_key: Provider key of the most recent transaction.
        category: Subscription category from keyword heuristics,
            or ``None``.
        sector: GICS sector when merchant classification was provided,
            else ``None``.
        first_detected_at: Date of the earliest transaction in the group.
        last_detected_at: Date of the latest transaction in the group.
        occurrence_count: Number of transactions in the pattern.
        details: Additional diagnostic information (amount consistency,
            interval regularity, intervals in days, keyword matches,
            etc.).
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
    transaction_ids: list[str]
    account_id: str
    provider_key: str
    category: str | None = None
    sector: str | None = None
    security_id: str | None = None
    first_detected_at: datetime | None = None
    last_detected_at: datetime | None = None
    occurrence_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)


# ── PatternDetector ─────────────────────────────────────────────────────


class PatternDetector:
    """Analyse transaction lists for recurring subscription patterns.

    Works on any list of transaction dicts that contain at minimum
    ``amount``, ``description``, and ``occurred_at`` keys.

    The detector is configurable at construction time via its parameters
    (see ``__init__``), making it suitable both for interactive tuning
    and for automated pipelines.

    Integration with merchant classification::

        detector = PatternDetector()
        results = detector.detect(
            transactions,
            classifications={
                merchant: classify_merchant(merchant)
                for merchant in detector.group_by_merchant(transactions)
            },
        )
    """

    def __init__(
        self,
        *,
        min_occurrences: int = _DEFAULT_MIN_OCCURRENCES,
        max_amount_variance_pct: Decimal = _DEFAULT_MAX_AMOUNT_VARIANCE_PCT,
        max_amount_absolute: Decimal = _DEFAULT_MAX_AMOUNT_ABSOLUTE,
        allow_zero_amount: bool = _DEFAULT_ALLOW_ZERO_AMOUNT,
    ) -> None:
        """Configure the pattern detector.

        Args:
            min_occurrences: Minimum number of occurrences required to
                consider a pattern (default 2).
            max_amount_variance_pct: Maximum allowable relative variance
                in amounts as a decimal fraction (default 0.05 = 5 %).
            max_amount_absolute: Maximum allowable absolute variance
                for small amounts (default EUR 2.00).  When the absolute
                deviation is at or below this threshold the amounts are
                considered consistent regardless of relative variance.
            allow_zero_amount: If ``True``, include transactions with
                an amount of zero when detecting patterns.
        """
        self.min_occurrences = min_occurrences
        self.max_amount_variance_pct = max_amount_variance_pct
        self.max_amount_absolute = max_amount_absolute
        self.allow_zero_amount = allow_zero_amount

    # ── Public API ─────────────────────────────────────────────────────

    def detect(
        self,
        transactions: list[dict[str, Any]],
        *,
        classifications: dict[str, Any] | None = None,
    ) -> list[PatternResult]:
        """Detect recurring patterns in a list of transactions.

        Args:
            transactions: List of transaction dicts.  Each dict must
                contain at least:
                - ``amount`` (``Decimal`` or numeric) — signed value
                - ``description`` (``str`` or ``None``)
                - ``occurred_at`` (``datetime``)
                - ``id`` (``str``)
                - ``account_id`` (``str``)
                - ``provider_key`` (``str``)
                - ``currency_code`` (``str``, optional — defaults to
                  ``"EUR"``)
            classifications: Optional dict mapping merchant name to
                either a ``MerchantClass`` instance or a plain dict
                with ``sector``, ``security_id``, and
                ``likelihood_score`` keys.  When provided these enrich
                the pattern results with sector data and a confidence
                boost.

        Returns:
            List of :class:`PatternResult` instances, one per merchant
            group that exhibits a recurring pattern.  Empty list when
            no patterns are found.

        Raises:
            ValueError: If ``transactions`` is ``None``.
        """
        if transactions is None:
            raise ValueError(_MSG_TRANSACTIONS_NONE)

        if not transactions:
            return []

        # 1. Filter outgoing transactions
        outgoing = [
            t
            for t in transactions
            if self._is_outgoing(t) and self._amount_ok(t)
        ]

        if not outgoing:
            return []

        # 2. Group by normalised merchant
        groups = self.group_by_merchant(outgoing)

        # 3. Analyse each merchant group
        results: list[PatternResult] = []
        for merchant, txns in groups.items():
            if len(txns) < self.min_occurrences:
                continue

            result = self._analyse_group(
                merchant, txns, classifications=classifications
            )
            if result is not None:
                results.append(result)

        # 4. Sort by detection score descending (most confident first)
        results.sort(key=lambda r: r.detection_score, reverse=True)

        return results

    def group_by_merchant(
        self,
        transactions: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Group transactions by their normalised merchant name.

        Uses the same normalisation logic as ``SubscriptionDetector``
        so results are consistent across the detection pipeline.

        Args:
            transactions: List of transaction dicts.

        Returns:
            Dict mapping normalised merchant name to the list of
            transactions attributed to that merchant.
        """
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for txn in transactions:
            merchant = _normalise_merchant(txn.get("description"))
            groups[merchant].append(txn)
        return groups

    # ── Internal helpers ───────────────────────────────────────────────

    @staticmethod
    def _is_outgoing(txn: dict[str, Any]) -> bool:
        """Check whether a transaction is an outgoing payment.

        A transaction is considered outgoing when:
        - Its ``amount`` is negative (outflow), *or*
        - Its ``transaction_type`` is a debit-like type.
        """
        amount = txn.get("amount")
        if amount is not None:
            try:
                val = Decimal(str(amount))
                if val < 0:
                    return True
            except (ValueError, TypeError):
                pass

        txn_type = txn.get("transaction_type", "")
        return bool(
            isinstance(txn_type, str)
            and txn_type.lower() in ("payment", "purchase", "fee", "withdrawal")
        )

    def _amount_ok(self, txn: dict[str, Any]) -> bool:
        """Check whether the transaction amount is usable for detection."""
        amount = txn.get("amount")
        if amount is None:
            return False
        try:
            val = Decimal(str(amount))
        except (ValueError, TypeError):
            return False
        return not (val == 0 and not self.allow_zero_amount)

    def _analyse_group(
        self,
        merchant: str,
        txns: list[dict[str, Any]],
        *,
        classifications: dict[str, Any] | None = None,
    ) -> PatternResult | None:
        """Analyse a single merchant group for recurring patterns.

        Returns ``None`` when the group does not form a meaningful
        pattern (e.g. amounts are completely inconsistent).
        """
        # Sort by date for interval computation
        txns_sorted = sorted(txns, key=lambda t: t["occurred_at"])
        amounts = [Decimal(str(t["amount"])) for t in txns_sorted]
        dates = [t["occurred_at"] for t in txns_sorted]
        descriptions = [t.get("description", "") or "" for t in txns_sorted]

        # Extract classification data if available
        classification: Any = (classifications or {}).get(merchant)
        sector: str | None = None
        security_id: str | None = None
        sector_boost: float = 0.0

        if classification is not None:
            # Handle both MerchantClass instances and plain dicts
            if hasattr(classification, "sector"):
                sector = classification.sector
                security_id = getattr(classification, "security_id", None)
                sector_boost = (
                    getattr(classification, "likelihood_score", 0.0) or 0.0
                )
            elif isinstance(classification, dict):
                sector = classification.get("sector")
                security_id = classification.get("security_id")
                sector_boost = (
                    classification.get("likelihood_score", 0.0) or 0.0
                )

        # Amount consistency
        amount_consistency = _amounts_are_consistent(amounts)

        # Detect step-change pattern (price changes over time)
        if amount_consistency == 0.0:
            step_score = _amounts_step_change_score([abs(a) for a in amounts])
            if step_score > 0.0:
                amount_consistency = step_score

        if amount_consistency == 0.0:
            return None

        # Intervals between consecutive transactions
        intervals_days: list[float] = []
        for i in range(1, len(dates)):
            if dates[i] and dates[i - 1]:
                diff = (dates[i] - dates[i - 1]).total_seconds() / 86400.0
                if diff > 0:
                    intervals_days.append(diff)

        # Detect frequency (with skipped-payment tolerance)
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

        # Keyword / category heuristics
        raw_text = " ".join(descriptions)
        has_keyword = any(_is_subscription_keyword(d) for d in descriptions)
        has_keyword = has_keyword or _is_subscription_keyword(raw_text)
        category = _classify_category(raw_text)

        # Compute confidence
        confidence, score = _compute_confidence_score(
            occurrence_count=len(txns),
            amount_consistency=amount_consistency,
            interval_regularity=interval_regularity,
            has_keyword=has_keyword,
            has_category=category is not None,
            sector_boost=sector_boost,
        )

        # Determine detection method
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

        # Most recent transaction for account / provider metadata
        latest = txns_sorted[-1]

        return PatternResult(
            merchant_name=merchant,
            raw_description=descriptions[-1] if descriptions else None,
            amount=abs(amounts[0]),
            currency_code=latest.get("currency_code", "EUR"),
            frequency_days=frequency_days,
            frequency_label=frequency_label,
            confidence=confidence,
            detection_score=score,
            detection_method=method,
            transaction_ids=[t["id"] for t in txns_sorted],
            account_id=latest["account_id"],
            provider_key=latest["provider_key"],
            category=category,
            sector=sector,
            security_id=security_id,
            first_detected_at=dates[0] if dates else None,
            last_detected_at=dates[-1] if dates else None,
            occurrence_count=len(txns),
            details={
                "amount_consistency": amount_consistency,
                "interval_regularity": interval_regularity,
                "intervals_days": [round(i, 1) for i in intervals_days],
                "has_keyword": has_keyword,
                "amounts": [str(a) for a in amounts],
                "sector_boost": sector_boost,
            },
        )

    # ── Config introspection ─────────────────────────────────────────

    @property
    def config(self) -> dict[str, Any]:
        """Return current detector configuration as a dict.

        Useful for logging, serialisation, or reproducing the same
        configuration in another session.
        """
        return {
            "min_occurrences": self.min_occurrences,
            "max_amount_variance_pct": str(self.max_amount_variance_pct),
            "max_amount_absolute": str(self.max_amount_absolute),
            "allow_zero_amount": self.allow_zero_amount,
        }


__all__ = [
    "PatternDetector",
    "PatternResult",
]
