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
    text = re.split(r"[,;\-—|]", text)[0].strip()

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
    """Check for explicit subscription-related keywords in a description."""
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
    *,
    sector_boost: float = 0.0,
) -> tuple[SubscriptionConfidence, float]:
    """Compute a confidence level and numeric score for a subscription.

    Args:
        occurrence_count: Number of matched occurrences.
        amount_consistency: 1.0 = exact, lower = more variance.
        interval_regularity: 1.0 = perfectly regular.
        has_keyword: Description contains subscription-related keywords.
        has_category: Merchant could be classified into a category.
        sector_boost: Additional boost from merchant classification
            (0.0-0.12 based on sector subscription likelihood).

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

    # Sector-based subscription likelihood boost (max: 0.12)
    score += sector_boost

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

        # 2. Run both merchant-based and clustering-based detection
        detected = await self._run_all_detection(
            transactions, min_occurrences=min_occurrences
        )

        # 3. Persist detected subscriptions
        persisted = await self._persist_subscriptions(detected)

        self._log.info(
            "subscription_detection_complete",
            found=len(persisted),
        )

        return persisted

    async def detect_with_clustering(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        min_occurrences: int = _MIN_OCCURRENCES,
    ) -> list[DetectedSubscription]:
        """Run detection with clustering-based pattern recognition.

        Like :meth:`detect` but emphasises the clustering pipeline.
        Equivalent to calling ``detect()`` — both approaches run together.

        Added in Phase 5 for callers that specifically want clustering.
        """
        return await self.detect(
            date_from=date_from,
            date_to=date_to,
            min_occurrences=min_occurrences,
        )

    async def analyze(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        min_occurrences: int = _MIN_OCCURRENCES,
        use_merchant_classifier: bool = True,
    ) -> list[dict[str, Any]]:
        """Analyze transactions for subscription patterns without persisting.

        Dry-run equivalent of :meth:`detect`.  Runs the full detection
        pipeline — merchant grouping, pattern clustering, cross-account
        matching, and merchant classification — but returns raw result
        dicts instead of saving to the database.

        Useful for previews, debugging, and API consumers that need to
        inspect findings before committing.

        Args:
            date_from: Earliest transaction date (default 365 days ago).
            date_to: Latest transaction date (default now).
            min_occurrences: Minimum occurrences to consider a pattern.
            use_merchant_classifier: Whether to enrich with merchant
                sector/classification data.

        Returns:
            List of detection result dicts (same shape as the dicts
            passed to ``_persist_subscriptions``).
        """
        if date_from is None:
            date_from = datetime.now(UTC) - timedelta(days=_DEFAULT_DAYS_BACK)
        if date_to is None:
            date_to = datetime.now(UTC)

        self._log.info(
            "subscription_analysis_start",
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )

        transactions = await self._fetch_outgoing_transactions(
            date_from, date_to
        )
        if not transactions:
            self._log.info("no_outgoing_transactions_found")
            return []

        detected = await self._run_all_detection(
            transactions,
            min_occurrences=min_occurrences,
            use_merchant_classifier=use_merchant_classifier,
        )

        self._log.info(
            "subscription_analysis_complete",
            found=len(detected),
        )

        return detected

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

    async def confirm_subscription(
        self,
        subscription_id: str,
        *,
        user_notes: str | None = None,
    ) -> DetectedSubscription | None:
        """Mark a subscription as confirmed by the user.

        Sets status to ``ACTIVE`` and optionally adds user notes.
        Returns ``None`` if the subscription doesn't exist or belongs
        to a different tenant.
        """
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

            sub.status = SubscriptionStatus.ACTIVE
            if user_notes is not None:
                # Append to existing notes rather than overwriting
                existing = sub.user_notes or ""
                note = f"[Confirmed] {user_notes}"
                sub.user_notes = f"{existing}\n{note}" if existing else note

            await session.commit()
            await session.refresh(sub)
            return sub

    async def ignore_subscription(
        self,
        subscription_id: str,
        *,
        reason: str | None = None,
    ) -> DetectedSubscription | None:
        """Mark a subscription as ignored by the user.

        Sets status to ``IGNORED`` and appends an optional ignore reason
        to user notes.  Returns ``None`` if the subscription doesn't
        exist or belongs to a different tenant.
        """
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

            sub.status = SubscriptionStatus.IGNORED
            if reason is not None:
                existing = sub.user_notes or ""
                note = f"[Ignored] {reason}"
                sub.user_notes = f"{existing}\n{note}" if existing else note

            await session.commit()
            await session.refresh(sub)
            return sub

    async def delete_subscription(
        self,
        subscription_id: str,
    ) -> bool:
        """Delete a detected subscription record.

        Returns ``True`` if the record was deleted, ``False`` if it
        didn't exist or belonged to a different tenant.
        """
        async with self._session_factory() as session:
            from finance_sync.db.repositories import (
                DetectedSubscriptionRepository,
            )

            repo = DetectedSubscriptionRepository(session)
            sub = await repo.get(subscription_id)

            if sub is None:
                return False
            if sub.tenant_id != self._tenant_id:
                return False

            await repo.delete(sub)
            await session.commit()
            return True

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
        classifications: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Analyze each merchant group for recurring subscription patterns.

        Args:
            groups: Transactions grouped by merchant name.
            min_occurrences: Minimum occurrences to consider a pattern.
            classifications: Optional pre-computed merchant classifications
                keyed by merchant name (from MerchantClassifier).
        """
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

            # Extract classification data if available
            classification = (classifications or {}).get(merchant)
            sector = classification.get("sector") if classification else None
            security_id = (
                classification.get("security_id") if classification else None
            )
            sector_boost = (
                classification.get("likelihood_score", 0.0)
                if classification
                else 0.0
            )

            analysis = self._analyze_merchant_group(
                merchant,
                payment_txns,
                sector=sector,
                security_id=security_id,
                sector_boost=sector_boost,
            )
            if analysis is not None:
                results.append(analysis)

        return results

    def _analyze_merchant_group(
        self,
        merchant: str,
        txns: list[dict[str, Any]],
        *,
        sector: str | None = None,
        security_id: str | None = None,
        sector_boost: float = 0.0,
    ) -> dict[str, Any] | None:
        """Analyze a single merchant's transactions for subscription patterns.

        Args:
            merchant: Normalised merchant name.
            txns: List of transaction dicts for this merchant.
            sector: GICS sector from merchant classification (optional).
            security_id: Linked security ID from merchant classifier (optional).
            sector_boost: Confidence boost from sector-based likelihood
                (0.0-0.12).
        """
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

        # Compute confidence with sector boost
        confidence, score = _compute_confidence_score(
            occurrence_count=len(txns),
            amount_consistency=amount_consistency,
            interval_regularity=interval_regularity,
            has_keyword=has_keyword,
            has_category=category is not None,
            sector_boost=sector_boost,
        )

        # Detection method — MERCHANT_CLASSIFICATION when sector is known
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
            "sector": sector,
            "security_id": security_id,
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
                "sector_boost": sector_boost,
            },
        }

    # ── Combined detection pipeline ─────────────────────────────────

    async def _run_all_detection(
        self,
        transactions: list[dict[str, Any]],
        *,
        min_occurrences: int,
        use_merchant_classifier: bool = True,
    ) -> list[dict[str, Any]]:
        """Run all detection strategies and merge results.

        Combines:
        1. Existing merchant-based grouping analysis.
        2. Clustering-based amount and period detection.
        3. Cross-account matching.
        4. Merchant classification via fundamentals/ETF metadata.

        Results are deduplicated by merchant name, preferring the
        higher-confidence entry.  Clustering results are enriched with
        merchant classification data (sector, security_id, sector_boost)
        before merging so the best entry — regardless of source — carries
        classification metadata.
        """
        results: list[dict[str, Any]] = []

        # 1. Merchant-based grouping
        merchant_groups = self._group_by_merchant(transactions)

        # 1b. Run merchant classifier if enabled
        classifications: dict[str, dict[str, Any]] | None = None
        if use_merchant_classifier:
            classifications = await self._classify_merchants(merchant_groups)

        merchant_results = await self._analyze_groups(
            merchant_groups,
            min_occurrences=min_occurrences,
            classifications=classifications,
        )
        results.extend(merchant_results)

        # 2. Clustering-based detection
        try:
            from finance_sync.services.pattern_clustering import (
                SubscriptionPatternEngine,
            )

            engine = SubscriptionPatternEngine(
                min_occurrences=min_occurrences,
            )
            cluster_results = engine.detect(transactions)

            # Enrich clustering results with merchant classification data
            if classifications:
                cluster_results = self._enrich_cluster_results(
                    cluster_results, classifications
                )

            results.extend(cluster_results)
        except Exception:
            self._log.warning("clustering_detection_failed", exc_info=True)

        # 3. Deduplicate by merchant name, keeping highest confidence
        #    and preferring entries with sector data when scores tie
        return self._deduplicate_results(results)

    def _enrich_cluster_results(
        self,
        cluster_results: list[dict[str, Any]],
        classifications: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Enrich clustering-based detection results with merchant
        classification data.

        Adds sector, security_id, and sector_boost (likelihood_score) to
        each cluster / cross-account result whose merchant name has a
        corresponding classification entry.

        Args:
            cluster_results: Patterns from SubscriptionPatternEngine.
            classifications: Merchant classification dict keyed by
                merchant name (from ``_classify_merchants``).

        Returns:
            The enriched results list (modified in place and returned).
        """
        for result in cluster_results:
            merchant = result.get("merchant_name", "")
            cls = classifications.get(merchant)
            if cls is None:
                continue
            sector = cls.get("sector")
            if sector:
                result["sector"] = sector
                result["security_id"] = cls.get("security_id")
                # Only upgrade detection method to MERCHANT_CLASSIFICATION
                # when sector data is present
                if result.get("detection_method") in (
                    DetectionMethod.AMOUNT_CLUSTER,
                    DetectionMethod.CROSS_ACCOUNT,
                    DetectionMethod.REGULAR_INTERVAL,
                ):
                    result["detection_method"] = (
                        DetectionMethod.MERCHANT_CLASSIFICATION
                    )

                # Re-compute confidence with sector boost
                sector_boost = cls.get("likelihood_score", 0.0)
                if sector_boost > 0:
                    details = result.get("details", {}) or {}
                    current_score = result.get("detection_score", 0.0) or 0.0
                    new_score = min(1.0, current_score + sector_boost)
                    # Update confidence if score crosses a threshold
                    from finance_sync.models.enums import (
                        SubscriptionConfidence,
                    )

                    if new_score >= 0.80:
                        result["confidence"] = SubscriptionConfidence.HIGH
                    elif new_score >= 0.50:
                        result["confidence"] = SubscriptionConfidence.MEDIUM
                    result["detection_score"] = new_score

                    # Record the boost in details
                    if details is not None:
                        details["sector_boost"] = sector_boost
                        details["sector"] = sector
                    result["details"] = details

        return cluster_results

    async def _classify_merchants(
        self,
        groups: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        """Run merchant classification on all merchant groups.

        Uses the MerchantClassifier to label each merchant with its
        GICS sector, ticker, and subscription likelihood. Falls back
        to category-based classification when fundamentals aren't available.

        Returns:
            Dict mapping merchant_name -> {
                'sector': str | None,
                'security_id': str | None,
                'likelihood_score': float,
                'ticker': str | None,
            }
        """
        classifications: dict[str, dict[str, Any]] = {}

        try:
            from finance_sync.services.merchant_classifier import (
                MerchantClassifier,
            )

            classifier = MerchantClassifier(
                uow=None
            )  # No UoW for now (sync-only classification)

            for merchant in groups:
                # Guess category from a representative description
                txns = groups[merchant]
                descriptions = [
                    t.get("description", "")
                    for t in txns
                    if t.get("description")
                ]
                raw_text = " ".join(descriptions)
                category = None
                if raw_text:
                    category = _classify_category(raw_text)

                result = await classifier.classify(
                    merchant,
                    category=category,
                    use_fundamentals=False,
                )

                classifications[merchant] = {
                    "sector": result.sector,
                    "security_id": result.security_id,
                    "likelihood_score": result.likelihood_score,
                    "ticker": result.ticker,
                    "subscription_likelihood": result.subscription_likelihood,
                    "source": result.source,
                }

            classified_count = sum(
                1 for c in classifications.values() if c["sector"] is not None
            )
            if classified_count:
                self._log.info(
                    "merchant_classification_complete",
                    total=len(classifications),
                    classified=classified_count,
                )

        except Exception:
            self._log.warning("merchant_classification_failed", exc_info=True)

        return classifications

    @staticmethod
    def _deduplicate_results(
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Deduplicate detection results by merchant name.

        When multiple results share the same merchant name, the one with
        the highest detection score is kept.  When scores are equal the
        entry that carries sector/security_id classification data (i.e.
        from merchant classification) wins, as it's richer.
        """
        if not results:
            return []

        # Scoring helper: higher score wins; ties broken by presence of
        # sector data (merchant-classification results are richer).
        def _entry_key(r: dict[str, Any]) -> tuple[float, int]:
            score = r.get("detection_score", 0.0) or 0.0
            has_sector = 1 if r.get("sector") else 0
            # Prefer entries with sector data (merchant-classified)
            return (score, has_sector)

        # Map merchant name -> best result
        best: dict[str, dict[str, Any]] = {}
        for r in results:
            merchant = r["merchant_name"]
            best_key = _entry_key(r)
            if merchant not in best or best_key > _entry_key(best[merchant]):
                best[merchant] = r

        # Preserve order: first seen wins for ties (after applying
        # the tiebreaker above, first-seen with sector data wins)
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in results:
            merchant = r["merchant_name"]
            if merchant not in seen:
                seen.add(merchant)
                deduped.append(best[merchant])

        return deduped

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
                    security_id=data.get("security_id"),
                    sector=data.get("sector"),
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
