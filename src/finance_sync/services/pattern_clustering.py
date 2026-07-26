"""Pattern clustering for subscription detection — time-series
and clustering methods.

Provides advanced pattern recognition beyond simple merchant-based grouping:

- **AmountClusterDetector**: Density-based clustering on transaction amounts to
  find groups of transactions that share similar values across different
  merchants and time periods — catches subscriptions whose prices changed or
  that appear
  under slightly different merchant names.

- **PeriodicPatternDetector**: Uses interval histogram analysis with peak
  detection to find dominant periodicities in a transaction time series — more
  robust than simple median-interval matching because it handles skipped/missed
  payments and overlapping frequency bands.

- **CrossAccountMatcher**: Links subscription patterns detected across different
  accounts and providers, identifying the same subscription billed through
  different instruments.

- **SubscriptionPatternEngine**: Orchestrates all detectors into a unified
  pipeline that returns structured pattern results with confidence scores.
"""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)

if TYPE_CHECKING:
    from datetime import datetime

logger = structlog.get_logger("finance_sync.services.pattern_clustering")

# ── Constants ──────────────────────────────────────────────────────────

_MIN_OCCURRENCES = 2
_MAX_LOOKBACK_DAYS = 365

# Amount clustering parameters
_AMOUNT_CLUSTER_EPS_PCT = Decimal("0.05")  # 5% relative tolerance
_AMOUNT_CLUSTER_EPS_ABS = Decimal(
    "2.00"
)  # €2 absolute tolerance for small amounts
_MIN_AMOUNT_CLUSTER_PTS = 2  # minimum points for a cluster

# Interval histogram parameters
_INTERVAL_MIN = 3  # minimum interval in days to consider
_INTERVAL_MAX = 385  # maximum interval (just over 1 year)
_SMOOTHING_WINDOW = 3  # moving average window for histogram smoothing
_PEAK_PROMINENCE_FACTOR = 0.3  # minimum peak height relative to max

# Frequency bands: (label, low_days, high_days, nominal_days)
_FREQUENCY_RANGES: list[tuple[str, int, int, int]] = [
    ("weekly", 6, 8, 7),
    ("biweekly", 13, 15, 14),
    ("monthly", 25, 35, 30),
    ("quarterly", 80, 100, 90),
    ("semiannual", 160, 200, 180),
    ("yearly", 345, 385, 365),
]

# Cross-account matching parameters
_MIN_AMOUNT_OVERLAP_PCT = Decimal("0.50")  # at least 50% amount overlap
_MAX_INTERVAL_MISMATCH_PCT = Decimal("0.20")  # max 20% interval difference
_MAX_DATE_OVERLAP_DAYS = 60  # max days between matched patterns' date ranges


# ── 1D Density-based clustering ────────────────────────────────────────


def _density_cluster_1d(
    values: list[Decimal],
    *,
    eps_pct: Decimal = _AMOUNT_CLUSTER_EPS_PCT,
    eps_abs: Decimal = _AMOUNT_CLUSTER_EPS_ABS,
    min_pts: int = _MIN_AMOUNT_CLUSTER_PTS,
) -> list[list[int]]:
    """Cluster 1-D numeric values using a simple density-based algorithm.

    Like DBSCAN on 1D data: two points are neighbours if their absolute
    difference is within *eps_abs* **or** within *eps_pct* of their mean.
    Core points have ≥ *min_pts* neighbours.

    Returns a list of clusters, each being a list of indices into *values*.
    Points that don't belong to any cluster (noise) are omitted.
    """
    n = len(values)
    if n < min_pts:
        return []

    # Build adjacency: neighbours[i] = set of j where |values[i]-values[j]| ≤ eps  # noqa: E501
    neighbours: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            a, b = abs(values[i]), abs(values[j])
            diff = abs(a - b)
            mean_ab = (a + b) / Decimal(2)
            # Check absolute tolerance first, then relative
            if diff <= eps_abs or (
                mean_ab > Decimal(0) and diff / mean_ab <= eps_pct
            ):
                neighbours[i].add(j)
                neighbours[j].add(i)

    # Core points: have ≥ min_pts neighbours (including self)
    core = {i for i in range(n) if len(neighbours[i]) + 1 >= min_pts}

    # Expand clusters from core points
    visited: set[int] = set()
    clusters: list[list[int]] = []

    for i in range(n):
        if i in visited or i not in core:
            continue
        # Start new cluster
        cluster: list[int] = []
        stack = [i]
        while stack:
            p = stack.pop()
            if p in visited:
                continue
            visited.add(p)
            cluster.append(p)
            # Add core neighbours for expansion
            if p in core:
                for nb in neighbours[p]:
                    if nb not in visited:
                        stack.append(nb)  # noqa: PERF401
        if len(cluster) >= min_pts:
            clusters.append(sorted(cluster))

    return clusters


# ── Amount cluster detection ────────────────────────────────────────────


class AmountCluster:
    """A cluster of transactions with similar amounts.

    Attributes:
        amount: Representative (median) amount for the cluster.
        transaction_indices: Indices into the original transaction list.
        count: Number of transactions in the cluster.
        total: Sum of absolute amounts.
    """

    def __init__(
        self,
        amount: Decimal,
        indices: list[int],
        count: int,
        total: Decimal,
    ) -> None:
        self.amount = amount
        self.transaction_indices = indices
        self.count = count
        self.total = total

    def __repr__(self) -> str:
        return f"<AmountCluster amount={self.amount} count={self.count}>"


def _median_decimal(values: list[Decimal]) -> Decimal:
    """Compute median of a list of Decimals."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 0:
        return Decimal(0)
    if n % 2 == 1:
        return sorted_vals[n // 2]
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / Decimal(2)


class AmountClusterDetector:
    """Detect transaction clusters by amount similarity.

    Groups transactions with similar amounts regardless of merchant name,
    catching subscriptions whose descriptions vary between banks or whose
    price shifted over time.
    """

    def __init__(
        self,
        *,
        eps_pct: Decimal = _AMOUNT_CLUSTER_EPS_PCT,
        eps_abs: Decimal = _AMOUNT_CLUSTER_EPS_ABS,
        min_points: int = _MIN_AMOUNT_CLUSTER_PTS,
    ) -> None:
        self._eps_pct = eps_pct
        self._eps_abs = eps_abs
        self._min_points = min_points

    def detect_clusters(
        self,
        transactions: list[dict[str, Any]],
    ) -> list[AmountCluster]:
        """Find amount-based clusters in transaction data.

        Args:
            transactions: List of transaction dicts, each containing at
                least ``amount`` (Decimal) and ``id`` (str).

        Returns:
            List of AmountCluster objects, sorted by count descending.
        """
        if len(transactions) < self._min_points:
            return []

        amounts = [t["amount"] for t in transactions]
        clusters = _density_cluster_1d(
            amounts,
            eps_pct=self._eps_pct,
            eps_abs=self._eps_abs,
            min_pts=self._min_points,
        )

        result: list[AmountCluster] = []
        for indices in clusters:
            cluster_amounts = [amounts[i] for i in indices]
            median_amt = _median_decimal(cluster_amounts)
            total_amt = sum(abs(a) for a in cluster_amounts)
            result.append(
                AmountCluster(
                    amount=median_amt,
                    indices=indices,
                    count=len(indices),
                    total=total_amt,
                )
            )

        result.sort(key=lambda c: c.count, reverse=True)
        return result


# ── Periodic pattern detection ──────────────────────────────────────────


class PeriodCandidate:
    """A detected periodic pattern in transaction dates.

    Attributes:
        period_days: Detected period in days.
        label: Human-readable frequency label (e.g. 'monthly').
        score: Confidence score for this period (0.0-1.0).
        peak_count: Number of transaction pairs with this interval.
    """

    def __init__(
        self,
        period_days: int,
        label: str | None,
        score: float,
        peak_count: int,
    ) -> None:
        self.period_days = period_days
        self.label = label
        self.score = score
        self.peak_count = peak_count

    def __repr__(self) -> str:
        return (
            f"<PeriodCandidate {self.label} "
            f"period={self.period_days}d "
            f"score={self.score:.2f}>"
        )


def _smooth_series(
    values: list[float], window: int = _SMOOTHING_WINDOW
) -> list[float]:
    """Smooth a 1D list with a centred moving average.

    Edge values are computed from a shrinking window so the output
    length matches the input.
    """
    n = len(values)
    if n < window or window < 2:
        return list(values)
    result: list[float] = []
    for i in range(n):
        left = max(0, i - window // 2)
        right = min(n, i + window // 2 + 1)
        result.append(sum(values[left:right]) / (right - left))
    return result


def _find_peaks(
    values: list[float],
    min_height: float = 0.0,
) -> list[int]:
    """Find local maxima (peaks) in a 1D series, including plateaus.

    Handles flat plateaus by selecting the centre index of each
    plateau region that qualifies as a peak.  Returns indices of
    peaks with value > *min_height*.
    """
    n = len(values)
    peaks: list[int] = []

    i = 0
    while i < n:
        if values[i] <= min_height:
            i += 1
            continue

        # Find the extent of a flat plateau (including single-point "plateaus")
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1

        # A plateau is a peak if its immediate left neighbour (if any) is
        # lower *and* its immediate right neighbour (if any) is lower.
        # At least one neighbour-side must exist and be lower — a completely
        # flat series (no lower point on either side) is not a peak.
        left_exists = i > 0
        right_exists = j < n - 1
        left_lower = left_exists and values[i] > values[i - 1]
        right_lower = right_exists and values[j] > values[j + 1]

        # Edge plateaus (no neighbour on one side) need the other side lower
        is_edge_left = i == 0
        is_edge_right = j == n - 1

        if (
            (is_edge_left and right_lower)
            or (is_edge_right and left_lower)
            or (left_lower and right_lower)
        ):
            # Add centre index of the plateau
            peaks.append((i + j) // 2)

        i = j + 1

    return peaks


def _interval_histogram(
    intervals: list[float],
    min_period: int = _INTERVAL_MIN,
    max_period: int = _INTERVAL_MAX,
) -> list[float]:
    """Build a histogram of interval frequencies.

    Returns a list of length (max_period - min_period + 1) where
    index ``i`` corresponds to period ``min_period + i`` days.
    """
    bin_count = max_period - min_period + 1
    hist = [0.0] * bin_count
    for interval in intervals:
        idx = round(interval) - min_period
        if 0 <= idx < bin_count:
            hist[idx] += 1.0
    return hist


def _detect_periods_from_intervals(
    intervals_days: list[float],
    *,
    min_occurrences: int = 2,
) -> list[PeriodCandidate]:
    """Detect periodic patterns from a list of day intervals.

    Uses interval histogram with smoothing and peak detection.
    Each significant peak is scored and mapped to a frequency label.

    Args:
        intervals_days: Sorted list of interval lengths in days.
        min_occurrences: Minimum peak count to consider.

    Returns:
        List of PeriodCandidate objects sorted by score descending.
    """
    if len(intervals_days) < min_occurrences:
        return []

    # Strategy: for each frequency band, count how many intervals fall
    # within it, then score the best band and report the top candidates.
    candidates: list[PeriodCandidate] = []

    # 1. Score each frequency band by how many intervals map to it
    band_scores: list[
        tuple[str, int, int, int, int]
    ] = []  # (label, low, high, nominal, count)
    for label, low, high, nominal in _FREQUENCY_RANGES:
        count = sum(1 for i in intervals_days if low <= round(i) <= high)
        if count >= min_occurrences:
            band_scores.append((label, low, high, nominal, count))

    if not band_scores:
        return []

    # 2. Sort bands by count descending
    band_scores.sort(key=lambda bs: bs[4], reverse=True)
    total_count = len(intervals_days)

    # 3. Build candidates from the top bands
    # Use the raw histogram to find the exact day peak within each band
    hist = _interval_histogram(intervals_days)
    smoothed = _smooth_series(hist, _SMOOTHING_WINDOW)
    max_smoothed = max(smoothed) if smoothed else 0.0

    for label, low, high, nominal, count in band_scores[:3]:  # top 3 bands max
        # Find the best exact period within this band
        best_period = nominal
        best_count = 0
        for days in range(low, high + 1):
            idx = days - _INTERVAL_MIN
            if 0 <= idx < len(hist) and hist[idx] > best_count:
                best_count = int(hist[idx])
                best_period = days

        # Score: combination of band density and occurrence count
        density = count / total_count if total_count > 0 else 0.0
        occurrence_factor = min(1.0, count / 12.0)
        score = density * 0.6 + occurrence_factor * 0.4

        # Boost score if the raw histogram peak in this band is well-defined
        if max_smoothed > 0:
            peak_idx = best_period - _INTERVAL_MIN
            if 0 <= peak_idx < len(smoothed):
                peak_ratio = smoothed[peak_idx] / max_smoothed
                score = score * 0.8 + peak_ratio * 0.2

        score = min(1.0, max(0.0, score))

        candidates.append(
            PeriodCandidate(
                period_days=best_period,
                label=label,
                score=round(score, 4),
                peak_count=best_count,
            )
        )

    # 4. Also check the raw histogram for other significant peaks outside
    #    standard bands that might indicate split intervals (e.g. 60-day
    #    as 2× monthly, 45-day as semi-monthly, etc.)  # noqa: RUF003
    peak_indices = _find_peaks(
        smoothed, min_height=max_smoothed * _PEAK_PROMINENCE_FACTOR
    )
    for idx in peak_indices:
        period_days = _INTERVAL_MIN + idx
        count = round(hist[idx])
        if count < min_occurrences:
            continue
        # Check if this period is already covered by a candidate
        is_covered = any(
            low <= period_days <= high
            for _label, low, high, _nominal, _cnt in band_scores
        )
        if is_covered:
            continue
        # Map this orphan period to the nearest frequency label
        label = _map_period_to_label(period_days)
        score = (
            min(1.0, smoothed[idx] / max_smoothed) if max_smoothed > 0 else 0.0
        )
        candidates.append(
            PeriodCandidate(
                period_days=period_days,
                label=label,
                score=round(score, 4),
                peak_count=count,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _map_period_to_label(period_days: int) -> str | None:
    """Map a period in days to the best-matching frequency label."""
    for label, low, high, _nominal in _FREQUENCY_RANGES:
        if low <= period_days <= high:
            return label
    return None


class PeriodicPatternDetector:
    """Detect periodic patterns in transaction date sequences.

    Uses interval histogram analysis with peak detection to find the
    dominant period(s) in a series of transaction dates — more robust
    than simple median-interval matching because it handles skipped
    payments and overlapping frequency bands.
    """

    def __init__(
        self,
        *,
        min_occurrences: int = _MIN_OCCURRENCES,
    ) -> None:
        self._min_occurrences = min_occurrences

    def detect_periods(
        self,
        transactions: list[dict[str, Any]],
    ) -> list[PeriodCandidate]:
        """Detect periodic patterns in a list of transactions.

        Args:
            transactions: Sorted transaction dicts with ``occurred_at``
                (datetime) fields.

        Returns:
            List of PeriodCandidate objects, sorted by score descending.
        """
        if len(transactions) < self._min_occurrences:
            return []

        # Extract dates
        dates: list[datetime] = []
        for t in transactions:
            dt = t.get("occurred_at")
            if dt is not None:
                dates.append(dt)

        if len(dates) < self._min_occurrences:
            return []

        dates.sort()

        # Compute consecutive intervals
        intervals: list[float] = []
        for i in range(1, len(dates)):
            diff = (dates[i] - dates[i - 1]).total_seconds() / 86400.0
            if _INTERVAL_MIN <= diff <= _INTERVAL_MAX:
                intervals.append(diff)

        return _detect_periods_from_intervals(
            intervals,
            min_occurrences=self._min_occurrences,
        )

    def compute_regularity(self, intervals_days: list[float]) -> float:
        """Compute interval regularity score from a list of day intervals.

        Returns a score from 0.0 (very irregular) to 1.0 (perfectly regular).
        Uses coefficient of variation on the interval list after detecting
        the dominant period.
        """
        if len(intervals_days) < 2:
            return 1.0 if len(intervals_days) == 1 else 0.0

        # Detect periods to find the best-fitting one
        periods = _detect_periods_from_intervals(
            intervals_days,
            min_occurrences=2,
        )

        if not periods:
            # Fall back to CV-based scoring
            mean_int = sum(intervals_days) / len(intervals_days)
            if mean_int <= 0:
                return 0.0
            variance = sum((d - mean_int) ** 2 for d in intervals_days)
            std_dev = math.sqrt(variance / (len(intervals_days) - 1))
            cv = std_dev / mean_int
            if cv <= 0.1:
                return 1.0
            if cv <= 0.25:
                return 0.7
            if cv <= 0.5:
                return 0.4
            return 0.1

        # Use the best period to evaluate regularity
        best = periods[0]
        # For each interval, check if it's a multiple of the dominant period
        if best.period_days <= 0:
            return 0.0

        matches = 0
        for interval in intervals_days:
            # Check if interval is close to a whole-number multiple of the period  # noqa: E501
            if best.period_days > 0:
                mult = round(interval / best.period_days)
                if mult >= 1:
                    expected = mult * best.period_days
                    deviation = (
                        abs(interval - expected) / expected
                        if expected > 0
                        else 1.0
                    )
                    if deviation <= 0.15:  # within 15%
                        matches += 1

        return matches / len(intervals_days) if intervals_days else 0.0


# ── Cross-account matching ─────────────────────────────────────────────


class CrossAccountMatch:
    """A subscription pattern detected across multiple accounts.

    Attributes:
        merchant_name: Normalised merchant name for the match.
        amount: Representative amount.
        frequency_label: Detected frequency.
        accounts: List of account IDs where the pattern appears.
        providers: List of provider keys where the pattern appears.
        confidence: Overall confidence for the cross-account match.
        source_groups: List of source pattern dicts that were merged.
    """

    def __init__(
        self,
        merchant_name: str,
        amount: Decimal,
        frequency_label: str | None,
        accounts: list[str],
        providers: list[str],
        confidence: SubscriptionConfidence,
        source_groups: list[dict[str, Any]],
    ) -> None:
        self.merchant_name = merchant_name
        self.amount = amount
        self.frequency_label = frequency_label
        self.accounts = accounts
        self.providers = providers
        self.confidence = confidence
        self.source_groups = source_groups

    def __repr__(self) -> str:
        return (
            f"<CrossAccountMatch {self.merchant_name!r} "
            f"across {len(self.accounts)} accounts "
            f"confidence={self.confidence}>"
        )


class CrossAccountMatcher:
    """Find subscription patterns that span multiple accounts/providers.

    A subscription may be billed through different accounts (e.g. personal
    vs business, credit card vs debit card). This matcher groups related
    patterns by amount similarity, interval proximity, and date range overlap.
    """

    def __init__(
        self,
        *,
        amount_overlap_pct: Decimal = _MIN_AMOUNT_OVERLAP_PCT,
        interval_mismatch_pct: Decimal = _MAX_INTERVAL_MISMATCH_PCT,
        date_overlap_days: int = _MAX_DATE_OVERLAP_DAYS,
    ) -> None:
        self._amount_overlap_pct = amount_overlap_pct
        self._interval_mismatch_pct = interval_mismatch_pct
        self._date_overlap_days = date_overlap_days

    def find_cross_account_matches(
        self,
        patterns: list[dict[str, Any]],
    ) -> list[CrossAccountMatch]:
        """Find cross-account matches among a list of pattern dicts.

        Each pattern dict must contain:
            - ``merchant_name`` (str)
            - ``amount`` (Decimal)
            - ``frequency_label`` (str | None)
            - ``frequency_days`` (int | None)
            - ``account_id`` (str | None)
            - ``provider_key`` (str | None)
            - ``first_detected_at`` (datetime)
            - ``last_detected_at`` (datetime)

        Args:
            patterns: List of pattern result dicts from the detector.

        Returns:
            List of CrossAccountMatch objects.
        """
        if len(patterns) < 2:
            return []

        # Group by normalised merchant name first
        merchant_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in patterns:
            merchant_groups[p["merchant_name"]].append(p)

        matches: list[CrossAccountMatch] = []
        for group in merchant_groups.values():
            if len(group) < 2:
                continue

            # Within the same merchant group, find cross-account links
            merged = self._merge_by_account(group)
            if merged:
                matches.extend(merged)

        # Also find amount-based cross-merchant links
        amount_matches = self._find_amount_cross_links(patterns)
        matches.extend(amount_matches)

        return matches

    def _merge_by_account(
        self,
        patterns: list[dict[str, Any]],
    ) -> list[CrossAccountMatch]:
        """Merge patterns with different accounts/providers but same merchant."""  # noqa: E501
        accounts_seen: set[str] = set()
        matches: list[CrossAccountMatch] = []

        for i, a in enumerate(patterns):
            acct_a = a.get("account_id") or ""
            if acct_a in accounts_seen:
                continue

            merged_group = [a]
            accounts_seen.add(acct_a)

            for b in patterns[i + 1 :]:
                acct_b = b.get("account_id") or ""
                if acct_b in accounts_seen:
                    continue

                # Check amount compatibility
                if not self._amounts_compatible(a["amount"], b["amount"]):
                    continue

                # Check interval compatibility
                if not self._intervals_compatible(
                    a.get("frequency_days"),
                    b.get("frequency_days"),
                ):
                    continue

                # Check date range overlap
                if not self._date_overlaps(
                    a.get("first_detected_at"),
                    a.get("last_detected_at"),
                    b.get("first_detected_at"),
                    b.get("last_detected_at"),
                ):
                    continue

                merged_group.append(b)
                accounts_seen.add(acct_b)

            if len(merged_group) >= 2:
                matches.append(self._build_match(merged_group))

        return matches

    def _find_amount_cross_links(
        self,
        patterns: list[dict[str, Any]],
    ) -> list[CrossAccountMatch]:
        """Find patterns with different merchants but same amount and interval.

        This catches cases where the same subscription service appears under
        different merchant names across accounts (e.g. 'Google *YouTube' vs
        'YOUTUBE PREMIUM').
        """
        if len(patterns) < 2:
            return []

        matches: list[CrossAccountMatch] = []
        used_patterns: set[int] = set()

        for i, a in enumerate(patterns):
            if i in used_patterns:
                continue
            group = [a]
            used_patterns.add(i)

            for j, b in enumerate(patterns[i + 1 :], start=i + 1):
                if j in used_patterns:
                    continue

                # Must have compatible amounts
                if not self._amounts_compatible(a["amount"], b["amount"]):
                    continue

                # Must have compatible intervals
                if not self._intervals_compatible(
                    a.get("frequency_days"),
                    b.get("frequency_days"),
                ):
                    continue

                # Must have different merchants
                if a["merchant_name"] == b["merchant_name"]:
                    continue

                # Must have different accounts
                if a.get("account_id") == b.get("account_id"):
                    continue

                group.append(b)
                used_patterns.add(j)

            if len(group) >= 2:
                matches.append(self._build_match(group))

        return matches

    @staticmethod
    def _amounts_compatible(a: Decimal, b: Decimal) -> bool:
        """Check if two amounts are close enough to be the same subscription."""
        abs_a, abs_b = abs(a), abs(b)
        if abs_a == Decimal(0) and abs_b == Decimal(0):
            return True
        if abs_a == Decimal(0) or abs_b == Decimal(0):
            return False
        ratio = min(abs_a, abs_b) / max(abs_a, abs_b)
        return ratio >= _MIN_AMOUNT_OVERLAP_PCT

    @staticmethod
    def _intervals_compatible(
        freq_a: int | None,
        freq_b: int | None,
    ) -> bool:
        """Check if two interval frequencies are compatible."""
        if freq_a is None or freq_b is None:
            return True  # cannot reject on missing data
        if freq_a == 0 or freq_b == 0:
            return True
        ratio = min(freq_a, freq_b) / max(freq_a, freq_b)
        return ratio >= (Decimal(1) - _MAX_INTERVAL_MISMATCH_PCT)

    @staticmethod
    def _date_overlaps(
        first_a: datetime | None,
        last_a: datetime | None,
        first_b: datetime | None,
        last_b: datetime | None,
    ) -> bool:
        """Check if two pattern date ranges overlap in time."""
        if (
            first_a is None
            or last_a is None
            or first_b is None
            or last_b is None
        ):
            return True  # cannot reject on missing data

        # Check gap between date ranges
        gap = max(
            (first_b - last_a).total_seconds() / 86400.0,
            (first_a - last_b).total_seconds() / 86400.0,
        )
        return not gap > _MAX_DATE_OVERLAP_DAYS

    @staticmethod
    def _build_match(group: list[dict[str, Any]]) -> CrossAccountMatch:
        """Build a CrossAccountMatch from a group of related patterns."""
        accounts = list({p.get("account_id") or "unknown" for p in group})
        providers = list({p.get("provider_key") or "unknown" for p in group})

        # Use the most common or first amount
        amount = group[0]["amount"]

        # Use the most common frequency label
        freq_labels = [
            p.get("frequency_label") for p in group if p.get("frequency_label")
        ]
        freq_label = (
            max(set(freq_labels), key=freq_labels.count)
            if freq_labels
            else None
        )

        # Confidence: boost for cross-account confirmation
        base_conf = group[0].get("confidence", SubscriptionConfidence.LOW)
        if base_conf == SubscriptionConfidence.LOW:
            confidence = SubscriptionConfidence.MEDIUM
        elif base_conf == SubscriptionConfidence.MEDIUM:
            confidence = SubscriptionConfidence.HIGH
        else:
            confidence = SubscriptionConfidence.HIGH

        # Merchant name: use the most common
        merchant_names = [p["merchant_name"] for p in group]
        merchant_name = max(set(merchant_names), key=merchant_names.count)

        return CrossAccountMatch(
            merchant_name=merchant_name,
            amount=amount,
            frequency_label=freq_label,
            accounts=accounts,
            providers=providers,
            confidence=confidence,
            source_groups=group,
        )


# ── Confidence scoring for cluster-based patterns ─────────────────────


def _compute_cluster_confidence(
    occurrence_count: int,
    amount_consistency: float,
    interval_regularity: float,
    cluster_size: int,
    has_cross_account_confirmation: bool = False,
) -> tuple[SubscriptionConfidence, float]:
    """Compute confidence for an amount-cluster-based detection.

    Factors:
        - Occurrence count (more = better, max 0.25)
        - Amount consistency within cluster (max 0.25)
        - Interval regularity (max 0.25)
        - Cluster size / total transactions (max 0.15)
        - Cross-account confirmation bonus (max 0.10)
    """
    score = 0.0

    # Occurrence count (max 0.25)
    if occurrence_count >= 12:
        score += 0.25
    elif occurrence_count >= 6:
        score += 0.20
    elif occurrence_count >= 4:
        score += 0.15
    elif occurrence_count >= 3:
        score += 0.10
    else:
        score += 0.05

    # Amount consistency (max 0.25)
    score += amount_consistency * 0.25

    # Interval regularity (max 0.25)
    score += interval_regularity * 0.25

    # Cluster density (max 0.15) — larger clusters within total = more likely subscription  # noqa: E501
    cluster_density = min(1.0, cluster_size / 10.0)
    score += cluster_density * 0.15

    # Cross-account bonus (max 0.10)
    if has_cross_account_confirmation:
        score += 0.10

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


# ── Orchestrator ───────────────────────────────────────────────────────


class SubscriptionPatternEngine:
    """Unified orchestrator for all pattern detection methods.

    Combines amount-based clustering, interval period detection, and
    cross-account matching into a single pipeline.

    Usage::

        engine = SubscriptionPatternEngine()
        patterns = engine.detect(all_transactions)
        for p in patterns:
            print(p["merchant_name"], p["confidence"])
    """

    def __init__(
        self,
        *,
        min_occurrences: int = _MIN_OCCURRENCES,
    ) -> None:
        self._amount_detector = AmountClusterDetector(
            min_points=min_occurrences
        )
        self._period_detector = PeriodicPatternDetector(
            min_occurrences=min_occurrences
        )
        self._cross_matcher = CrossAccountMatcher()
        self._min_occurrences = min_occurrences

    def detect(
        self,
        transactions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run the full pattern detection pipeline.

        Steps:
            1. Detect amount-based clusters.
            2. For each cluster, analyse periodicity and compute confidence.
            3. Find cross-account matches.
            4. Return unified pattern results.

        Args:
            transactions: List of transaction dicts (same shape as
                ``SubscriptionDetector._fetch_outgoing_transactions``).

        Returns:
            List of pattern result dicts, each containing:
                - merchant_name
                - amount
                - currency_code
                - frequency_days / frequency_label
                - confidence / detection_score
                - detection_method
                - transaction_ids
                - account_id / provider_key
                - category
                - first_detected_at / last_detected_at
                - occurrence_count
                - details
        """
        if not transactions:
            return []

        self._log = logger.bind(txns=len(transactions))
        self._log.info("pattern_engine_start")

        # Step 1: Amount cluster detection
        clusters = self._amount_detector.detect_clusters(transactions)
        self._log.debug("amount_clusters_found", count=len(clusters))

        # Step 2: Analyse each cluster
        patterns: list[dict[str, Any]] = []
        for cluster in clusters:
            cluster_txns = [
                transactions[i] for i in cluster.transaction_indices
            ]
            pattern = self._analyse_cluster_pattern(cluster, cluster_txns)
            if pattern is not None:
                patterns.append(pattern)

        # Step 3: Cross-account matching
        if len(patterns) >= 2:
            cross_matches = self._cross_matcher.find_cross_account_matches(
                patterns
            )
            for match in cross_matches:
                # Add cross-account confirmed patterns
                cross_pattern = self._build_cross_account_pattern(match)
                if cross_pattern is not None:
                    patterns.append(cross_pattern)

        self._log.info("pattern_engine_complete", patterns=len(patterns))
        return patterns

    def _analyse_cluster_pattern(
        self,
        cluster: AmountCluster,
        txns: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Analyse a single amount cluster for subscription patterns."""
        if len(txns) < self._min_occurrences:
            return None

        # Sort by date
        txns_sorted = sorted(txns, key=lambda t: t["occurred_at"])
        amounts = [t["amount"] for t in txns_sorted]
        dates = [t["occurred_at"] for t in txns_sorted]

        # Amount consistency within cluster
        amount_consistency = self._compute_cluster_amount_consistency(amounts)
        if amount_consistency == 0.0:
            return None

        # Detect periods from intervals
        intervals_days: list[float] = []
        for i in range(1, len(dates)):
            diff = (dates[i] - dates[i - 1]).total_seconds() / 86400.0
            if _INTERVAL_MIN <= diff <= _INTERVAL_MAX:
                intervals_days.append(diff)

        periods = _detect_periods_from_intervals(
            intervals_days,
            min_occurrences=2,
        )

        frequency_days: int | None = None
        frequency_label: str | None = None
        if periods:
            frequency_days = periods[0].period_days
            frequency_label = periods[0].label

        # Interval regularity
        interval_regularity = self._period_detector.compute_regularity(
            intervals_days
        )

        # Collect descriptions for keyword analysis
        descriptions = [t.get("description", "") for t in txns_sorted]
        raw_descriptions = " ".join(descriptions)

        # Category hints
        from finance_sync.services.subscription_detector import (
            _classify_category,
            _is_subscription_keyword,
        )

        has_keyword = any(_is_subscription_keyword(d) for d in descriptions)
        has_keyword = has_keyword or _is_subscription_keyword(raw_descriptions)
        category = _classify_category(raw_descriptions)

        # Confidence
        confidence, score = _compute_cluster_confidence(
            occurrence_count=len(txns),
            amount_consistency=amount_consistency,
            interval_regularity=interval_regularity,
            cluster_size=cluster.count,
        )

        # Detection method
        if frequency_label is not None and amount_consistency > 0.5:
            method = DetectionMethod.AMOUNT_CLUSTER
        elif frequency_label is not None:
            method = DetectionMethod.REGULAR_INTERVAL
        else:
            method = DetectionMethod.SIMILAR_AMOUNT

        # Build merchant name from the most common normalised merchant in the cluster  # noqa: E501
        from finance_sync.services.subscription_detector import (
            _normalise_merchant,
        )

        merchants = [
            _normalise_merchant(t.get("description", "")) for t in txns_sorted
        ]
        merchant_name = max(set(merchants), key=merchants.count)

        return {
            "merchant_name": merchant_name,
            "raw_description": descriptions[-1] if descriptions else None,
            "amount": cluster.amount,
            "currency_code": txns[0]["currency_code"],
            "frequency_days": frequency_days,
            "frequency_label": frequency_label,
            "confidence": confidence,
            "detection_method": method,
            "status": SubscriptionStatus.ACTIVE,
            "transaction_ids": [t["id"] for t in txns],
            "account_id": txns[0].get("account_id"),
            "provider_key": txns[0].get("provider_key"),
            "category": category,
            "first_detected_at": dates[0],
            "last_detected_at": dates[-1],
            "occurrence_count": len(txns),
            "detection_score": score,
            "details": {
                "amount_consistency": amount_consistency,
                "interval_regularity": interval_regularity,
                "intervals_days": [round(i, 1) for i in intervals_days],
                "periods": [
                    {
                        "days": p.period_days,
                        "label": p.label,
                        "score": p.score,
                    }
                    for p in periods
                ],
                "cluster_size": cluster.count,
                "has_keyword": has_keyword,
                "amounts": [str(a) for a in amounts],
            },
        }

    def _build_cross_account_pattern(
        self,
        match: CrossAccountMatch,
    ) -> dict[str, Any] | None:
        """Build a pattern result dict from a CrossAccountMatch."""
        if not match.source_groups:
            return None

        source = match.source_groups[0]
        all_ids: list[str] = []
        all_dates: list[datetime] = []
        for g in match.source_groups:
            ids = g.get("transaction_ids", [])
            all_ids.extend(ids if isinstance(ids, list) else [ids])
            fd = g.get("first_detected_at")
            ld = g.get("last_detected_at")
            if fd:
                all_dates.append(fd)
            if ld:
                all_dates.append(ld)

        if not all_dates:
            return None

        return {
            "merchant_name": match.merchant_name,
            "raw_description": source.get("raw_description"),
            "amount": match.amount,
            "currency_code": source.get("currency_code", "EUR"),
            "frequency_days": source.get("frequency_days"),
            "frequency_label": match.frequency_label,
            "confidence": match.confidence,
            "detection_method": DetectionMethod.CROSS_ACCOUNT,
            "status": SubscriptionStatus.ACTIVE,
            "transaction_ids": all_ids,
            "account_id": match.accounts[0]
            if match.accounts
            else source.get("account_id"),
            "provider_key": match.providers[0]
            if match.providers
            else source.get("provider_key"),
            "category": source.get("category"),
            "first_detected_at": min(all_dates),
            "last_detected_at": max(all_dates),
            "occurrence_count": len(all_ids),
            "detection_score": 0.0,  # cross-account patterns derive confidence from their sources  # noqa: E501
            "details": {
                "cross_account_match": True,
                "accounts": match.accounts,
                "providers": match.providers,
                "source_count": len(match.source_groups),
            },
        }

    @staticmethod
    def _compute_cluster_amount_consistency(amounts: list[Decimal]) -> float:
        """Compute amount consistency within a cluster.

        Since the cluster already groups similar amounts, this measures
        how tightly grouped they are.  Returns 1.0 for perfect consistency,
        degrading to 0.0.
        """
        if len(amounts) < 2:
            return 1.0

        from finance_sync.services.subscription_detector import (
            _amounts_are_consistent,
        )

        # Use the existing consistency checker
        base_score = _amounts_are_consistent(amounts)

        # Additional cluster-specific metric: within-cluster variance
        abs_amounts = [abs(a) for a in amounts]
        mean_amt = sum(abs_amounts) / Decimal(str(len(abs_amounts)))
        if mean_amt == Decimal(0):
            return 1.0

        max_dev = max(abs(a - mean_amt) for a in abs_amounts)
        variance_ratio = max_dev / mean_amt if mean_amt > 0 else Decimal(0)

        # If amounts are extremely tight (variance < 1%), boost score
        if variance_ratio <= Decimal("0.01"):
            return 1.0

        return base_score
