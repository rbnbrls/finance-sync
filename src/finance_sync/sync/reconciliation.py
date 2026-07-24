"""Core reconciliation detection logic — in-memory transaction comparison.

This module provides two pure-data detection functions that operate on
lists of canonical transaction data (typically from connector output or
database snapshots):

1. **detect_missing** — compare two connector data sets and find
   transactions present in one but not the other (cross-connector or
   cross-period reconciliation).

2. **detect_duplicates** — examine a single data set for transactions
   that appear more than once, either by exact external ID match or
   by heuristic amount + date proximity.

Every function returns structured result types carrying enough
context (transaction references, amounts, descriptions, confidence)
for downstream consumers to take action — emit outbox messages,
notify users, or update reconciliation run findings.

Usage::

    from finance_sync.sync.reconciliation import detect_missing, detect_duplicates

    missing = detect_missing(txns_a, txns_b, connector_a="bunq", connector_b="trading212")
    for item in missing.in_a_not_b:
        print(f"Missing in B: {item.external_transaction_id} ({item.amount})")

    dups = detect_duplicates(txns, connector="bunq")
    for dup in dups:
        print(f"Duplicate: {dup.transaction_a.id} ~ {dup.transaction_b.id}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

import structlog

from finance_sync.connectors.models import CanonicalTransactionData

# ── Public result types ───────────────────────────────────────────────

logger = structlog.get_logger("finance_sync.sync.reconciliation")

_DEFAULT_KEY_FN: Callable[[CanonicalTransactionData], Any] = (
    lambda t: t.external_transaction_id
)
_DEFAULT_DUPLICATE_THRESHOLD_HOURS = 48


# ── Dataclass result types ────────────────────────────────────────────


@dataclass(frozen=True)
class MissingTransaction:
    """A transaction that exists in one data set but is absent from another.

    Carries enough context for downstream actions such as outbox emission
    or dashboard alerting.
    """

    external_transaction_id: str
    """Provider's unique ID for this transaction."""

    provider_key: str
    """Connector / provider name (e.g. ``'bunq'``)."""

    account_id: str | None = None
    """External account ID the transaction belongs to (if available)."""

    amount: Decimal = Decimal("0")
    """Signed transaction amount."""

    currency_code: str = "EUR"
    """ISO-4217 currency code."""

    occurred_at: datetime | None = None
    """When the transaction occurred."""

    description: str | None = None
    """Human-readable description."""

    transaction_type: str | None = None
    """Normalised transaction type."""

    status: str | None = None
    """Transaction status (e.g. ``'booked'``, ``'pending'``)."""

    source: str | None = None
    """Which side this came from — set to ``'a'`` or ``'b'`` upstream."""

    @classmethod
    def from_canonical(
        cls,
        txn: CanonicalTransactionData,
        *,
        source: str | None = None,
    ) -> MissingTransaction:
        """Build a ``MissingTransaction`` from a canonical data model."""
        return cls(
            external_transaction_id=txn.external_transaction_id,
            provider_key=txn.provider_key,
            account_id=txn.external_account_id,
            amount=txn.amount,
            currency_code=txn.currency_code,
            occurred_at=txn.occurred_at,
            description=txn.description,
            transaction_type=txn.transaction_type,
            status=txn.status,
            source=source,
        )


@dataclass(frozen=True)
class MissingResult:
    """Summary of a cross-connector gap analysis."""

    in_a_not_b: list[MissingTransaction] = field(default_factory=list)
    """Transactions found in connector A but missing from B."""

    in_b_not_a: list[MissingTransaction] = field(default_factory=list)
    """Transactions found in connector B but missing from A."""

    matched_count: int = 0
    """Number of transactions that matched between the two data sets."""

    total_a: int = 0
    """Total transactions in data set A."""

    total_b: int = 0
    """Total transactions in data set B."""

    @property
    def total_findings(self) -> int:
        """Total number of missing-transaction findings."""
        return len(self.in_a_not_b) + len(self.in_b_not_a)

    @property
    def connector_a_only_pct(self) -> float:
        """Percentage of A's transactions that are missing from B."""
        if self.total_a == 0:
            return 0.0
        return round(len(self.in_a_not_b) / self.total_a * 100, 2)

    @property
    def connector_b_only_pct(self) -> float:
        """Percentage of B's transactions that are missing from A."""
        if self.total_b == 0:
            return 0.0
        return round(len(self.in_b_not_a) / self.total_b * 100, 2)


@dataclass(frozen=True)
class DuplicateTransaction:
    """A pair of transactions that appear to be duplicates of each other."""

    transaction_a: CanonicalTransactionData
    """First transaction in the duplicate pair."""

    transaction_b: CanonicalTransactionData
    """Second transaction in the duplicate pair."""

    match_reason: str
    """Why these were flagged: ``'exact_external_id'``, ``'amount_and_date'``."""

    confidence: float = 0.5
    """Confidence score between 0.0 and 1.0."""

    diff_hours: float = 0.0
    """Hours between the two transactions' occurrence timestamps."""

    amount_diff: Decimal = Decimal("0")
    """Absolute difference in amounts between the two transactions."""

    same_description: bool = False
    """Whether both transactions share the same description text."""

    same_provider: bool = False
    """Whether both transactions came from the same connector provider."""


# ── Public API ────────────────────────────────────────────────────────


def detect_missing(
    connector_a: list[CanonicalTransactionData],
    connector_b: list[CanonicalTransactionData],
    *,
    connector_a_name: str = "source_a",
    connector_b_name: str = "source_b",
    key: Callable[
        [CanonicalTransactionData], Any
    ] = _DEFAULT_KEY_FN,
) -> MissingResult:
    """Find transactions present in one data set but missing from the other.

    Compares two lists of canonical transactions using *key* (default:
    ``external_transaction_id``).  Returns structured results that show
    which transactions from each side have no counterpart on the other.

    Args:
        connector_a:  First list of canonical transactions.
        connector_b:  Second list of canonical transactions.
        connector_a_name:
            Human-readable label for connector A (used in log context).
        connector_b_name:
            Human-readable label for connector B (used in log context).
        key:
            Callable that extracts the comparison key from a
            ``CanonicalTransactionData``.  Defaults to
            ``external_transaction_id``.

    Returns:
        A ``MissingResult`` with per-side missing lists and match stats.
    """
    log = logger.bind(
        connector_a=connector_a_name,
        connector_b=connector_b_name,
        total_a=len(connector_a),
        total_b=len(connector_b),
    )

    if not connector_a and not connector_b:
        log.info("detect_missing_both_empty")
        return MissingResult(total_a=0, total_b=0)

    # Build key → transaction maps
    map_a: dict[Any, list[CanonicalTransactionData]] = {}
    for txn in connector_a:
        map_a.setdefault(key(txn), []).append(txn)

    map_b: dict[Any, list[CanonicalTransactionData]] = {}
    for txn in connector_b:
        map_b.setdefault(key(txn), []).append(txn)

    keys_a = set(map_a)
    keys_b = set(map_b)

    only_a_keys = keys_a - keys_b
    only_b_keys = keys_b - keys_a

    in_a_not_b: list[MissingTransaction] = []
    for k in only_a_keys:
        for txn in map_a[k]:
            in_a_not_b.append(
                MissingTransaction.from_canonical(txn, source="a")
            )

    in_b_not_a: list[MissingTransaction] = []
    for k in only_b_keys:
        for txn in map_b[k]:
            in_b_not_a.append(
                MissingTransaction.from_canonical(txn, source="b")
            )

    # Count matched transactions by key: for each key that appears in both
    # sets, the number that can be paired is the smaller of the two counts.
    matched_count = 0
    common_keys = keys_a & keys_b
    for k in common_keys:
        matched_count += min(len(map_a[k]), len(map_b[k]))

    result = MissingResult(
        in_a_not_b=in_a_not_b,
        in_b_not_a=in_b_not_a,
        matched_count=matched_count,
        total_a=len(connector_a),
        total_b=len(connector_b),
    )

    log.info(
        "detect_missing_complete",
        in_a_not_b=len(result.in_a_not_b),
        in_b_not_a=len(result.in_b_not_a),
        matched=result.matched_count,
    )

    return result


def detect_duplicates(
    transactions: list[CanonicalTransactionData],
    *,
    connector: str = "unknown",
    threshold_hours: int = _DEFAULT_DUPLICATE_THRESHOLD_HOURS,
) -> list[DuplicateTransaction]:
    """Find duplicate transactions within a single data set.

    Detects two kinds of duplicates:

    1. **Exact** — two or more transactions sharing the same
       ``external_transaction_id`` (always flagged).
    2. **Heuristic** — transactions whose amounts match exactly AND
       whose occurrence timestamps fall within *threshold_hours* of each
       other (potential duplicates from re-ingestion or near-simultaneous
       bookings).

    Args:
        transactions:  List of canonical transactions to examine.
        connector:
            Connector label for log context.
        threshold_hours:
            Max hours between two transactions' occurrence dates to
            consider them heuristic-close duplicates (default 48).

    Returns:
        A list of ``DuplicateTransaction`` findings, each describing a
        pair of suspiciously similar transactions.
    """
    log = logger.bind(connector=connector, total=len(transactions))

    if not transactions:
        log.info("detect_duplicates_empty")
        return []

    findings: list[DuplicateTransaction] = []
    seen_pairs: set[tuple[int, int]] = set()

    # ── Phase 1: Exact duplicates (same external_transaction_id) ────
    by_ext_id: dict[str, list[CanonicalTransactionData]] = {}
    for txn in transactions:
        by_ext_id.setdefault(txn.external_transaction_id, []).append(txn)

    for ext_id, group in by_ext_id.items():
        if len(group) < 2:
            continue
        log.debug("exact_duplicate_found", external_id=ext_id, count=len(group))
        # Report every unique pair within the group
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pair_key = (id(group[i]), id(group[j]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                diff = abs(group[i].occurred_at - group[j].occurred_at)
                findings.append(
                    DuplicateTransaction(
                        transaction_a=group[i],
                        transaction_b=group[j],
                        match_reason="exact_external_id",
                        confidence=1.0,
                        diff_hours=diff.total_seconds() / 3600,
                        amount_diff=abs(
                            (group[i].amount or Decimal(0))
                            - (group[j].amount or Decimal(0))
                        ),
                        same_description=_same_desc(group[i], group[j]),
                        same_provider=group[i].provider_key
                        == group[j].provider_key,
                    )
                )

    # ── Phase 2: Heuristic duplicates (amount + date proximity) ────
    threshold_td = timedelta(hours=threshold_hours)
    for i in range(len(transactions)):
        for j in range(i + 1, len(transactions)):
            a = transactions[i]
            b = transactions[j]

            # Skip if already flagged as exact duplicate
            pair_key = (id(a), id(b))
            if pair_key in seen_pairs:
                continue

            # Same external ID is already checked in Phase 1 — skip here
            if a.external_transaction_id == b.external_transaction_id:
                continue

            # Amount must match exactly for heuristic detection
            if (a.amount or Decimal(0)) != (b.amount or Decimal(0)):
                continue

            # Date proximity check
            time_diff = abs(a.occurred_at - b.occurred_at)
            if time_diff > threshold_td:
                continue

            seen_pairs.add(pair_key)

            # Compute confidence
            same_prov = a.provider_key == b.provider_key
            same_desc = _same_desc(a, b)
            confidence = _heuristic_confidence(same_prov, same_desc)

            findings.append(
                DuplicateTransaction(
                    transaction_a=a,
                    transaction_b=b,
                    match_reason="amount_and_date",
                    confidence=confidence,
                    diff_hours=time_diff.total_seconds() / 3600,
                    amount_diff=Decimal("0"),
                    same_description=same_desc,
                    same_provider=same_prov,
                )
            )

    log.info(
        "detect_duplicates_complete",
        exact=sum(
            1 for f in findings if f.match_reason == "exact_external_id"
        ),
        heuristic=sum(
            1 for f in findings if f.match_reason == "amount_and_date"
        ),
        total=len(findings),
    )

    return findings


# ── Internal helpers ──────────────────────────────────────────────────


def _same_desc(
    a: CanonicalTransactionData,
    b: CanonicalTransactionData,
) -> bool:
    """Check whether two transactions have the same description (case-insensitive)."""
    if not a.description and not b.description:
        return True
    if not a.description or not b.description:
        return False
    return a.description.lower().strip() == b.description.lower().strip()


def _heuristic_confidence(
    same_provider: bool,
    same_description: bool,
) -> float:
    """Compute a confidence score for a heuristic duplicate pair.

    Base: 0.5
    - Cross-provider: +0.2
    - Same description: +0.2
    - Same provider + different description: +0.1

    Returns a value in [0.5, 0.9].
    """
    score = 0.5
    if not same_provider:
        score += 0.2  # Cross-provider is more suspicious
    elif not same_description:
        score += 0.1  # Same provider, diff desc — mild suspicion
    if same_description:
        score += 0.2
    return round(min(score, 0.9), 2)
