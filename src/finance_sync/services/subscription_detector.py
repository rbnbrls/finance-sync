"""Subscription detection service — identifies recurring transactions.

Uses pattern recognition to detect subscriptions from transaction history
by analyzing amount consistency, interval regularity, and optionally
classifying merchants via fundamentals/securities metadata.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
    )

    from finance_sync.models.detected_subscription import DetectedSubscription

logger = structlog.get_logger("finance_sync.services.subscription_detector")

# ── Constants ──────────────────────────────────────────────────────────

_DEFAULT_DAYS_BACK = 365  # Scan up to 1 year by default
_MIN_OCCURRENCES = 2  # Minimum transactions to consider a pattern
_MAX_AMOUNT_VARIANCE_PCT = Decimal("0.05")  # 5% variance allowed
_MAX_AMOUNT_ABSOLUTE = Decimal(
    "2.00"
)  # Or €2 absolute variance for small amounts

# Standard frequency bands (in days) with tolerance
_FREQUENCY_BANDS: dict[str, tuple[int, int, int]] = {
    "weekly": (6, 8, 7),
    "biweekly": (13, 15, 14),
    "monthly": (25, 35, 30),
    "quarterly": (80, 100, 90),
    "semiannual": (160, 200, 180),
    "yearly": (345, 385, 365),
}

# Common subscription keywords (case-insensitive patterns)
_SUBSCRIPTION_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        r"\bsubscription\b",
        r"\bsub\b",
        r"\bpremium\b",
        r"\bpro\b\s",
        r"\bunlimited\b",
        r"\bmonthly\b",
        r"\brenewal\b",
        r"\bmembership\b",
        r"\brecurring\b",
        r"\bautopay\b",
        r"\bdirect.?debit\b",
    ]
]

# Category heuristics based on merchant description keywords
_CATEGORY_KEYWORDS: dict[str, list[re.Pattern[str]]] = {
    "streaming": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\bnetflix\b",
            r"\bspotify\b",
            r"\bapple.?music\b",
            r"\bdisney[+.]?\b",
            r"\bhbo\b",
            r"\bhulu\b",
            r"\bprime.?video\b",
            r"\byoutube.?premium\b",
            r"\bamc[+.]?\b",
            r"\bparamount[+.]?\b",
        ]
    ],
    "software": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\bgoogle.?workspace\b",
            r"\bmicrosoft.?365\b",
            r"\boffice.?365\b",
            r"\bdropbox\b",
            r"\bgithub\b",
            r"\bgitlab\b",
            r"\bnotion\b",
            r"\bfigma\b",
            r"\badobe\b",
            r"\bcreative.?cloud\b",
            r"\bslack\b",
            r"\bzoom\b",
            r"\bjira\b",
            r"\bconfluence\b",
            r"\bdatadog\b",
            r"\bnew.?relic\b",
            r"\bdigital.?ocean\b",
            r"\baws\b",
            r"\bopenai\b",
            r"\bchatgpt\b",
            r"\bmidjourney\b",
            r"\bclaude\b",
        ]
    ],
    "utilities": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\belectricit[ey]\b",
            r"\bgas\b",
            r"\bwater\b",
            r"\binternet\b",
            r"\bbroadband\b",
            r"\bphone\b",
            r"\bmobile\b",
            r"\benergy\b",
            r"\bpower\b",
        ]
    ],
    "fitness": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\bgym\b",
            r"\bfitness\b",
            r"\bpeloton\b",
            r"\byoga\b",
            r"\bcrossfit\b",
        ]
    ],
    "insurance": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\binsurance\b",
            r"\bhealth.?care\b",
            r"\bdental\b",
            r"\blife.?insurance\b",
        ]
    ],
    "news_media": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\bnew(?:s)?paper\b",
            r"\bmagazine\b",
            r"\bnyt\b",
            r"\bwall.?street.?journal\b",
            r"\bmedium\b",
            r"\bsubstack\b",
        ]
    ],
    "donations": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\bdonat(?:ion|e)\b",
            r"\bpatreon\b",
            r"\bko.?fi\b",
            r"\bbuymeacoffee\b",
            r"\bkickstarter\b",
        ]
    ],
    "cloud_storage": [
        re.compile(r, re.IGNORECASE)
        for r in [
            r"\bgoogle.?drive\b",
            r"\bicloud\b",
            r"\bdropbox\b",
            r"\bone.?drive\b",
            r"\bbox\b",
        ]
    ],
}


# ── Merchant name normalisation ────────────────────────────────────────


def _normalise_merchant(description: str | None) -> str:
    """Extract and normalise a merchant name from a transaction description.

    Strips common prefixes (e.g. 'POS', 'DEB', 'DIRECT DEBIT'),
    payment metadata (card numbers, reference numbers), and normalises
    whitespace.
    """
    if not description:
        return "Unknown Merchant"

    text = description.strip()

    # Remove common prefixes
    for prefix in [
        r"^POS\s+",
        r"^DEB\s+",
        r"^DIRECT\s+DEBIT\s+",
        r"^DD\s+",
        r"^SEPA\s+",
        r"^SO\s+",
        r"^CARD\s+PAYMENT\s+",
        r"^CARD\s+",
        r"^BETALING\s+",
        r"^I?\s*DEAL\s+",
        r"^ONLINE\s+",
        r"^WEB\s+",
    ]:
        text = re.sub(prefix, "", text, flags=re.IGNORECASE)

    # Remove reference/transaction numbers
    text = re.sub(
        r"\b(?:REF|TRX|TXN|TRANS|ID|NR)[.:]?\s*\w{6,}",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b\d{10,}\b", "", text)

    # Normalise whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Take first meaningful segment (up to first comma/semicolon/dash)
    text = re.split(r"[,;--|]", text)[0].strip()

    # Title-case for consistency
    text = text.title()

    # Fallback
    if not text:
        return "Unknown Merchant"

    return text[:128]


def _classify_category(description: str | None) -> str | None:
    """Classify a transaction into a subscription category based on keywords."""
    if not description:
        return None

    for category, patterns in _CATEGORY_KEYWORDS.items():
        for pattern in patterns:
            if pattern.search(description):
                return category
    return None


def _is_subscription_keyword(description: str | None) -> bool:
    """Check if a description contains explicit subscription-related keywords."""
    if not description:
        return False
    for pattern in _SUBSCRIPTION_KEYWORDS:
        if pattern.search(description):
            return True
    return False


def _detect_frequency(
    intervals_days: list[float],
) -> tuple[int | None, str | None]:
    """Detect the most likely frequency from a list of day intervals.

    Returns (interval_days, frequency_label) or (None, None) if no pattern
    matches.
    """
    if not intervals_days:
        return None, None

    median_interval = median(intervals_days)

    # Score each frequency band
    best_label: str | None = None
    best_interval: int | None = None
    best_distance = float("inf")

    for label, (low, high, nominal) in _FREQUENCY_BANDS.items():
        if low <= median_interval <= high:
            distance = abs(median_interval - nominal)
            if distance < best_distance:
                best_distance = distance
                best_label = label
                best_interval = nominal
        elif label == "monthly":
            # Also check for monthly with higher tolerance (25-38 days)
            if 25 <= median_interval <= 38:
                distance = abs(median_interval - 30)
                if distance < best_distance:
                    best_distance = distance
                    best_label = label
                    best_interval = 30

    return best_interval, best_label


def _compute_confidence_score(
    occurrence_count: int,
    amount_consistency: float,
    interval_regularity: float,
    has_keyword: bool,
    has_category: bool,
) -> tuple[SubscriptionConfidence, float]:
    """Compute a confidence level and numeric score for a subscription.

    Args:
        occurrence_count: Number of matched occurrences.
        amount_consistency: 1.0 = exact, lower = more variance.
        interval_regularity: 1.0 = perfectly regular.
        has_keyword: Description contains subscription-related keywords.
        has_category: Merchant could be classified into a category.

    Returns:
        Tuple of (confidence_level, score_0_to_1).
    """
    score = 0.0

    # Occurrence count (max contribution: 0.30)
    if occurrence_count >= 12:
        score += 0.30
    elif occurrence_count >= 6:
        score += 0.25
    elif occurrence_count >= 4:
        score += 0.20
    elif occurrence_count >= 3:
        score += 0.15
    else:
        score += 0.08

    # Amount consistency (max: 0.25)
    score += amount_consistency * 0.25

    # Interval regularity (max: 0.25)
    score += interval_regularity * 0.25

    # Keyword bonus (max: 0.12)
    if has_keyword:
        score += 0.12

    # Category classification bonus (max: 0.08)
    if has_category:
        score += 0.08

    # Clamp
    score = min(score, 1.0)

    # Map to confidence level
    if score >= 0.80:
        confidence = SubscriptionConfidence.HIGH
    elif score >= 0.50:
        confidence = SubscriptionConfidence.MEDIUM
    else:
        confidence = SubscriptionConfidence.LOW

    return confidence, score


def _amounts_are_consistent(
    amounts: list[Decimal],
) -> float:
    """Check if a list of amounts are consistent (same nominal value).

    Returns a consistency score: 1.0 for exact match, lower for variance.
    """
    if len(amounts) < 2:
        return 1.0

    abs_amounts = [abs(a) for a in amounts]
    mean_amt = sum(abs_amounts) / Decimal(str(len(abs_amounts)))

    if mean_amt == Decimal(0):
        # All amounts are zero — perfectly consistent
        return 1.0

    max_dev = Decimal(0)
    for a in abs_amounts:
        dev = abs(a - mean_amt)
        if dev > max_dev:
            max_dev = dev

    # Check absolute variance first (for small amounts)
    if max_dev <= _MAX_AMOUNT_ABSOLUTE:
        return 1.0

    # Check relative variance
    variance_pct = max_dev / mean_amt
    if variance_pct <= _MAX_AMOUNT_VARIANCE_PCT:
        return 1.0

    # Score degrades with variance
    if variance_pct <= Decimal("0.15"):
        return 0.6
    if variance_pct <= Decimal("0.30"):
        return 0.3

    return 0.0


# ── Service ────────────────────────────────────────────────────────────


class SubscriptionDetector:
    """Detect, persist, and manage recurring subscription transactions.

    Usage::

        svc = SubscriptionDetector(
            session_factory=container.session_factory,
            tenant_id="tenant_1",
        )
        subscriptions = await svc.detect()
        print(f"Found {len(subscriptions)} subscriptions")
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._log = logger.bind(tenant_id=tenant_id)

    # ── Public API ───────────────────────────────────────────────────

    async def detect(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        min_occurrences: int = _MIN_OCCURRENCES,
    ) -> list[DetectedSubscription]:
        """Run subscription detection on transaction history.

        Analyzes outgoing transactions for recurring patterns and persists
        the results as DetectedSubscription records.

        Args:
            date_from: Earliest transaction date (default 365 days ago).
            date_to: Latest transaction date (default now).
            min_occurrences: Minimum occurrences to consider a pattern.

        Returns:
            List of newly detected subscriptions.
        """
        if date_from is None:
            date_from = datetime.now(UTC) - timedelta(days=_DEFAULT_DAYS_BACK)
        if date_to is None:
            date_to = datetime.now(UTC)

        self._log.info(
            "subscription_detection_start",
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )

        # 1. Fetch outgoing transactions
        transactions = await self._fetch_outgoing_transactions(
            date_from, date_to
        )
        self._log.debug(
            "fetched_outgoing_transactions", count=len(transactions)
        )

        if not transactions:
            self._log.info("no_outgoing_transactions_found")
            return []

        # 2. Group by normalised merchant
        merchant_groups = self._group_by_merchant(transactions)

        # 3. Analyze each group for recurring patterns
        detected = await self._analyze_groups(
            merchant_groups,
            min_occurrences=min_occurrences,
        )

        # 4. Persist detected subscriptions
        persisted = await self._persist_subscriptions(detected)

        self._log.info(
            "subscription_detection_complete",
            found=len(persisted),
        )

        return persisted

    async def list_subscriptions(
        self,
        *,
        status: str | None = None,
        confidence: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DetectedSubscription]:
        """Return detected subscriptions for the tenant.

        Supports optional filtering by status and confidence.
        """
        from sqlalchemy import select

        from finance_sync.models.detected_subscription import (
            DetectedSubscription,
        )

        async with self._session_factory() as session:
            stmt = (
                select(DetectedSubscription).where(
                    DetectedSubscription.tenant_id == self._tenant_id
                )  # type: ignore[attr-defined]
            )

            if status:
                stmt = stmt.where(
                    DetectedSubscription.status == status  # type: ignore[attr-defined]
                )
            if confidence:
                stmt = stmt.where(
                    DetectedSubscription.confidence == confidence  # type: ignore[attr-defined]
                )

            stmt = (
                stmt.order_by(
                    DetectedSubscription.last_detected_at.desc()  # type: ignore[attr-defined]
                )
                .offset(offset)
                .limit(limit)
            )

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_subscription(
        self,
        subscription_id: str,
        *,
        status: str | None = None,
        category: str | None = None,
        user_notes: str | None = None,
    ) -> DetectedSubscription | None:
        """Update a detected subscription's status, category, or notes."""
        async with self._session_factory() as session:
            from finance_sync.db.repositories import (
                DetectedSubscriptionRepository,
            )

            repo = DetectedSubscriptionRepository(session)
            sub = await repo.get(subscription_id)

            if sub is None:
                return None
            if sub.tenant_id != self._tenant_id:
                return None

            if status is not None:
                sub.status = status  # type: ignore[assignment]
            if category is not None:
                sub.category = category
            if user_notes is not None:
                sub.user_notes = user_notes

            await session.commit()
            await session.refresh(sub)
            return sub

    # ── Transaction fetching ─────────────────────────────────────────

    async def _fetch_outgoing_transactions(
        self,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch outgoing (negative amount) transactions from the DB.

        Returns plain dicts for analysis — we don't need full ORM objects.
        """
        from sqlalchemy import select

        from finance_sync.models.transaction import Transaction

        async with self._session_factory() as session:
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
                    Transaction.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                    Transaction.amount < 0,  # type: ignore[attr-defined]
                    Transaction.occurred_at >= date_from,  # type: ignore[attr-defined]
                    Transaction.occurred_at <= date_to,  # type: ignore[attr-defined]
                    Transaction.status.in_(["booked", "pending"]),  # type: ignore[attr-defined]
                )
                .order_by(Transaction.occurred_at.asc())  # type: ignore[attr-defined]
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

    # ── Grouping ─────────────────────────────────────────────────────

    def _group_by_merchant(
        self,
        transactions: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Group transactions by normalised merchant name."""
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for txn in transactions:
            merchant = _normalise_merchant(txn.get("description"))
            groups[merchant].append(txn)
        return groups

    # ── Analysis ─────────────────────────────────────────────────────

    async def _analyze_groups(
        self,
        groups: dict[str, list[dict[str, Any]]],
        *,
        min_occurrences: int,
    ) -> list[dict[str, Any]]:
        """Analyze each merchant group for recurring subscription patterns."""
        results: list[dict[str, Any]] = []

        for merchant, txns in groups.items():
            if len(txns) < min_occurrences:
                continue

            # Only look at outgoing payments (purchase, payment, fee types)
            payment_txns = [
                t
                for t in txns
                if t["transaction_type"]
                in ("payment", "purchase", "fee", "other")
            ]
            if len(payment_txns) < min_occurrences:
                continue

            analysis = self._analyze_merchant_group(merchant, payment_txns)
            if analysis is not None:
                results.append(analysis)

        return results

    def _analyze_merchant_group(
        self,
        merchant: str,
        txns: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Analyze a single merchant's transactions for subscription patterns."""
        # Sort transactions by date for interval computation
        txns_sorted = sorted(txns, key=lambda t: t["occurred_at"])
        amounts = [t["amount"] for t in txns_sorted]
        dates = [t["occurred_at"] for t in txns_sorted]
        descriptions = [t.get("description", "") for t in txns_sorted]

        # Amount consistency check
        amount_consistency = _amounts_are_consistent(amounts)
        if amount_consistency == 0.0:
            return None

        # Calculate intervals between consecutive transactions
        intervals_days: list[float] = []
        for i in range(1, len(dates)):
            if dates[i] and dates[i - 1]:
                diff = (dates[i] - dates[i - 1]).total_seconds() / 86400.0
                if diff > 0:
                    intervals_days.append(diff)

        # Detect frequency
        frequency_days, frequency_label = _detect_frequency(intervals_days)

        # Calculate interval regularity
        interval_regularity = 0.0
        if intervals_days:
            # Lower CV = more regular
            mean_interval = sum(intervals_days) / len(intervals_days)
            if mean_interval > 0 and len(intervals_days) > 1:
                variance = sum((d - mean_interval) ** 2 for d in intervals_days)
                std_dev = (variance / (len(intervals_days) - 1)) ** 0.5
                cv = std_dev / mean_interval  # Coefficient of variation
                # Map: CV <= 0.1 -> 1.0, CV <= 0.25 -> 0.7, CV <= 0.5 -> 0.4
                if cv <= 0.1:
                    interval_regularity = 1.0
                elif cv <= 0.25:
                    interval_regularity = 0.7
                elif cv <= 0.5:
                    interval_regularity = 0.4
                else:
                    interval_regularity = 0.1

        # Keyword / category checks
        has_keyword = any(_is_subscription_keyword(d) for d in descriptions)
        # Check if any description has the merchant name itself as keyword
        # (the normalised merchant may not appear in raw text)
        raw_descriptions = " ".join(descriptions)
        has_keyword = has_keyword or _is_subscription_keyword(raw_descriptions)
        category = _classify_category(raw_descriptions)

        # Compute confidence
        confidence, score = _compute_confidence_score(
            occurrence_count=len(txns),
            amount_consistency=amount_consistency,
            interval_regularity=interval_regularity,
            has_keyword=has_keyword,
            has_category=category is not None,
        )

        # Detection method
        if amount_consistency >= 1.0 and frequency_label is not None:
            method = DetectionMethod.EXACT_AMOUNT
        elif amount_consistency > 0.0 and frequency_label is not None:
            method = DetectionMethod.SIMILAR_AMOUNT
        elif interval_regularity > 0.5:
            method = DetectionMethod.REGULAR_INTERVAL
        else:
            method = DetectionMethod.EXACT_AMOUNT

        # Build result dict
        return {
            "merchant_name": merchant,
            "raw_description": descriptions[-1] if descriptions else None,
            "amount": amounts[0],
            "currency_code": txns[0]["currency_code"],
            "frequency_days": frequency_days,
            "frequency_label": frequency_label,
            "confidence": confidence,
            "detection_method": method,
            "status": SubscriptionStatus.ACTIVE,
            "transaction_ids": [t["id"] for t in txns],
            "account_id": txns[0]["account_id"],
            "provider_key": txns[0]["provider_key"],
            "category": category,
            "first_detected_at": dates[0],
            "last_detected_at": dates[-1],
            "occurrence_count": len(txns),
            "detection_score": score,
            "details": {
                "amount_consistency": amount_consistency,
                "interval_regularity": interval_regularity,
                "intervals_days": [round(i, 1) for i in intervals_days],
                "has_keyword": has_keyword,
                "amounts": [str(a) for a in amounts],
            },
        }

    # ── Persistence ──────────────────────────────────────────────────

    async def _persist_subscriptions(
        self,
        detected: list[dict[str, Any]],
    ) -> list[DetectedSubscription]:
        """Persist detected subscriptions to the database.

        Deduplicates against existing subscriptions by merchant name.
        """
        if not detected:
            return []

        async with self._session_factory() as session:
            from sqlalchemy import select

            from finance_sync.models.detected_subscription import (
                DetectedSubscription,
            )

            # Fetch existing subscription merchant names for this tenant
            existing_stmt = select(DetectedSubscription.merchant_name).where(
                DetectedSubscription.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
                DetectedSubscription.status.in_(  # type: ignore[attr-defined]
                    [
                        SubscriptionStatus.ACTIVE.value,
                        SubscriptionStatus.PAUSED.value,
                        SubscriptionStatus.UNKNOWN.value,
                    ]
                ),
            )
            existing_result = await session.execute(existing_stmt)
            existing_merchants = {row[0] for row in existing_result.all()}

            persisted: list[DetectedSubscription] = []
            for data in detected:
                if data["merchant_name"] in existing_merchants:
                    self._log.debug(
                        "subscription_already_exists",
                        merchant=data["merchant_name"],
                    )
                    continue

                sub = DetectedSubscription(
                    tenant_id=self._tenant_id,
                    merchant_name=data["merchant_name"],
                    raw_description=data["raw_description"],
                    amount=data["amount"],
                    currency_code=data["currency_code"],
                    frequency_days=data["frequency_days"],
                    frequency_label=data["frequency_label"],
                    confidence=data["confidence"],
                    detection_method=data["detection_method"],
                    status=data["status"],
                    transaction_ids=data["transaction_ids"],
                    account_id=data["account_id"],
                    provider_key=data["provider_key"],
                    category=data["category"],
                    first_detected_at=data["first_detected_at"],
                    last_detected_at=data["last_detected_at"],
                    occurrence_count=data["occurrence_count"],
                    detection_score=data["detection_score"],
                    details=data["details"],
                )
                session.add(sub)
                persisted.append(sub)

            if persisted:
                await session.commit()
                for sub in persisted:
                    await session.refresh(sub)

            return persisted
