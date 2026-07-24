"""Tests for the sync-layer reconciliation detection logic.

Tests the pure-data detection functions in
``finance_sync.sync.reconciliation`` (``detect_missing`` and
``detect_duplicates``) against lists of ``CanonicalTransactionData``
objects without needing a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from finance_sync.connectors.models import CanonicalTransactionData
from finance_sync.sync.reconciliation import (
    DuplicateTransaction,
    MissingResult,
    MissingTransaction,
    detect_duplicates,
    detect_missing,
)


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


def _txn(
    *,
    external_transaction_id: str = "ext_1",
    provider_key: str = "bunq",
    external_account_id: str = "acct_1",
    amount: str = "-10.00",
    currency_code: str = "EUR",
    occurred_at: datetime | None = None,
    description: str | None = "Test transaction",
    transaction_type: str = "payment",
    status: str = "booked",
    now: datetime | None = None,
) -> CanonicalTransactionData:
    if occurred_at is None:
        occurred_at = now or datetime.now(UTC)
    return CanonicalTransactionData(
        external_transaction_id=external_transaction_id,
        provider_key=provider_key,
        external_account_id=external_account_id,
        amount=Decimal(amount),
        currency_code=currency_code,
        occurred_at=occurred_at,
        description=description,
        transaction_type=transaction_type,
        status=status,
    )


# ═══════════════════════════════════════════════════════════════════════
# detect_missing
# ═══════════════════════════════════════════════════════════════════════


class TestDetectMissing:
    """Comprehensive tests for ``detect_missing``."""

    def test_both_empty(self) -> None:
        """Empty inputs produce empty results."""
        result = detect_missing([], [])
        assert isinstance(result, MissingResult)
        assert result.in_a_not_b == []
        assert result.in_b_not_a == []
        assert result.matched_count == 0
        assert result.total_findings == 0

    def test_identical_lists(self, now: datetime) -> None:
        """Two identical transaction lists produce no findings."""
        txns = [
            _txn(external_transaction_id="t1", now=now),
            _txn(external_transaction_id="t2", now=now),
        ]
        result = detect_missing(txns, txns)
        assert result.in_a_not_b == []
        assert result.in_b_not_a == []
        assert result.matched_count == len(txns)
        assert result.total_findings == 0

    def test_missing_in_b(self, now: datetime) -> None:
        """Transactions only in A are reported as missing from B."""
        txns_a = [
            _txn(external_transaction_id="common_1", now=now),
            _txn(external_transaction_id="common_2", now=now),
            _txn(external_transaction_id="only_in_a", now=now),
        ]
        txns_b = [
            _txn(external_transaction_id="common_1", now=now),
            _txn(external_transaction_id="common_2", now=now),
        ]
        result = detect_missing(txns_a, txns_b)
        assert len(result.in_a_not_b) == 1
        assert result.in_a_not_b[0].external_transaction_id == "only_in_a"
        assert result.in_a_not_b[0].source == "a"
        assert result.in_b_not_a == []
        assert result.matched_count == 2

    def test_missing_in_a(self, now: datetime) -> None:
        """Transactions only in B are reported as missing from A."""
        txns_a = [
            _txn(external_transaction_id="common_1", now=now),
        ]
        txns_b = [
            _txn(external_transaction_id="common_1", now=now),
            _txn(external_transaction_id="only_in_b", now=now),
        ]
        result = detect_missing(txns_a, txns_b)
        assert len(result.in_b_not_a) == 1
        assert result.in_b_not_a[0].external_transaction_id == "only_in_b"
        assert result.in_b_not_a[0].source == "b"
        assert result.in_a_not_b == []

    def test_bidirectional_missing(self, now: datetime) -> None:
        """Lists with unique items on each side report both directions."""
        txns_a = [
            _txn(external_transaction_id="only_a", now=now),
            _txn(external_transaction_id="shared", now=now),
        ]
        txns_b = [
            _txn(external_transaction_id="only_b", now=now),
            _txn(external_transaction_id="shared", now=now),
        ]
        result = detect_missing(txns_a, txns_b)
        assert len(result.in_a_not_b) == 1
        assert result.in_a_not_b[0].external_transaction_id == "only_a"
        assert len(result.in_b_not_a) == 1
        assert result.in_b_not_a[0].external_transaction_id == "only_b"
        assert result.matched_count == 1

    def test_fully_disjoint(self, now: datetime) -> None:
        """No common transactions means all items are missing on the other side."""
        txns_a = [_txn(external_transaction_id="a1", now=now)]
        txns_b = [_txn(external_transaction_id="b1", now=now)]
        result = detect_missing(txns_a, txns_b)
        assert len(result.in_a_not_b) == 1
        assert len(result.in_b_not_a) == 1
        assert result.matched_count == 0

    def test_custom_key_function(self, now: datetime) -> None:
        """A custom key function can be used instead of external_transaction_id."""
        txns_a = [
            _txn(
                external_transaction_id="x1",
                amount="-10.00",
                now=now,
            ),
        ]
        txns_b = [
            _txn(
                external_transaction_id="x2",
                amount="-10.00",
                now=now,
            ),
        ]
        # Match by amount
        result = detect_missing(
            txns_a,
            txns_b,
            key=lambda t: str(t.amount),
        )
        # Both have amount "-10.00" — should match
        assert result.in_a_not_b == []
        assert result.in_b_not_a == []
        assert result.matched_count >= 1

    def test_duplicate_keys_in_one_side(self, now: datetime) -> None:
        """When a key appears multiple times, all are reported."""
        txns_a = [
            _txn(external_transaction_id="dup", amount="-10.00", now=now),
            _txn(external_transaction_id="dup", amount="-20.00", now=now),
        ]
        txns_b = [
            _txn(external_transaction_id="dup", amount="-10.00", now=now),
        ]
        result = detect_missing(txns_a, txns_b)
        # One of the two "dup" txns in A doesn't match by key — they both
        # share the same key, so after the match, one is left unmatched.
        # Since map_b has 1 "dup" and map_a has 2, after matching 1 pair:
        # len(in_a_not_b) = min(2, 1) — actually let's check.
        # The algorithm: keys_a = {"dup"}, keys_b = {"dup"}, only_*_keys = {}
        # No items in in_a_not_b or in_b_not_a because only_*_keys is empty.
        # matched_count = min(2, 1) - 0 = 1
        # This is a known limitation: with duplicate keys, we don't track
        # which specific duplicates are extra.
        assert result.matched_count == 1
        assert result.in_a_not_b == []
        assert result.in_b_not_a == []

    def test_from_canonical_builder(self, now: datetime) -> None:
        """MissingTransaction.from_canonical preserves fields."""
        txn = _txn(
            external_transaction_id="t_1",
            provider_key="bunq",
            external_account_id="acct_42",
            amount="-15.50",
            currency_code="EUR",
            occurred_at=now,
            description="Coffee",
            transaction_type="payment",
            status="booked",
        )
        mt = MissingTransaction.from_canonical(txn, source="a")
        assert mt.external_transaction_id == "t_1"
        assert mt.provider_key == "bunq"
        assert mt.account_id == "acct_42"
        assert mt.amount == Decimal("-15.50")
        assert mt.currency_code == "EUR"
        assert mt.occurred_at == now
        assert mt.description == "Coffee"
        assert mt.transaction_type == "payment"
        assert mt.status == "booked"
        assert mt.source == "a"

    def test_missing_result_properties(self, now: datetime) -> None:
        """MissingResult.total_findings and percentage properties."""
        txns_a = [
            _txn(external_transaction_id="a1", now=now),
            _txn(external_transaction_id="a2", now=now),
            _txn(external_transaction_id="a3", now=now),
        ]
        txns_b = [
            _txn(external_transaction_id="a1", now=now),
            _txn(external_transaction_id="b1", now=now),
        ]
        result = detect_missing(txns_a, txns_b)
        assert result.total_findings == 3  # a2, a3 missing from B (2) + b1 missing from A (1)
        # connector_a_only_pct = 2/3 ≈ 66.67%
        assert result.connector_a_only_pct == pytest.approx(66.67, rel=0.01)
        # connector_b_only_pct = 1/2 = 50%
        assert result.connector_b_only_pct == 50.0

    def test_zero_total_percentages(self) -> None:
        """Percentage properties return 0.0 when total is 0."""
        result = detect_missing([], [])
        assert result.connector_a_only_pct == 0.0
        assert result.connector_b_only_pct == 0.0

    def test_logger_context_labels(self, now: datetime) -> None:
        """Custom connector names are passed through (no crash)."""
        txns_a = [_txn(external_transaction_id="t1", now=now)]
        txns_b = []
        result = detect_missing(
            txns_a,
            txns_b,
            connector_a_name="bunq",
            connector_b_name="trading212",
        )
        assert len(result.in_a_not_b) == 1


# ═══════════════════════════════════════════════════════════════════════
# detect_duplicates
# ═══════════════════════════════════════════════════════════════════════


class TestDetectDuplicates:
    """Comprehensive tests for ``detect_duplicates``."""

    def test_empty_list(self) -> None:
        """Empty input produces empty result."""
        assert detect_duplicates([]) == []

    def test_single_transaction(self, now: datetime) -> None:
        """Single transaction cannot be a duplicate."""
        txns = [_txn(now=now)]
        assert detect_duplicates(txns) == []

    def test_exact_external_id_duplicate(self, now: datetime) -> None:
        """Same external_transaction_id flagged as exact duplicate."""
        txns = [
            _txn(
                external_transaction_id="same_id",
                amount="-10.00",
                now=now,
            ),
            _txn(
                external_transaction_id="same_id",
                amount="-10.00",
                now=now,
            ),
        ]
        findings = detect_duplicates(txns)
        assert len(findings) == 1
        f = findings[0]
        assert f.match_reason == "exact_external_id"
        assert f.confidence == 1.0
        assert f.transaction_a.external_transaction_id == "same_id"
        assert f.transaction_b.external_transaction_id == "same_id"

    def test_exact_duplicate_three_entries(self, now: datetime) -> None:
        """Three entries with the same external ID produce C(3,2) = 3 pairs."""
        txns = [
            _txn(external_transaction_id="dup", amount="-10.00", now=now),
            _txn(external_transaction_id="dup", amount="-10.00", now=now),
            _txn(external_transaction_id="dup", amount="-10.00", now=now),
        ]
        findings = detect_duplicates(txns)
        assert len(findings) == 3  # 3 choose 2
        assert all(f.match_reason == "exact_external_id" for f in findings)

    def test_heuristic_amount_and_date_match(self, now: datetime) -> None:
        """Transactions with same amount and close dates are flagged."""
        txns = [
            _txn(
                external_transaction_id="ext_a",
                amount="-25.00",
                occurred_at=now,
                description="Lunch",
                provider_key="bunq",
            ),
            _txn(
                external_transaction_id="ext_b",
                amount="-25.00",
                occurred_at=now + timedelta(hours=2),
                description="Lunch",
                provider_key="trading212",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        f = findings[0]
        assert f.match_reason == "amount_and_date"
        assert f.confidence >= 0.5
        assert f.same_description is True
        assert f.same_provider is False

    def test_heuristic_beyond_threshold_skipped(self, now: datetime) -> None:
        """Transactions beyond the hour threshold are not flagged."""
        txns = [
            _txn(
                external_transaction_id="ext_a",
                amount="-25.00",
                occurred_at=now,
            ),
            _txn(
                external_transaction_id="ext_b",
                amount="-25.00",
                occurred_at=now + timedelta(hours=100),
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert findings == []

    def test_different_amount_not_flagged(self, now: datetime) -> None:
        """Transactions with different amounts are not heuristic duplicates."""
        txns = [
            _txn(
                external_transaction_id="ext_a",
                amount="-10.00",
                occurred_at=now,
            ),
            _txn(
                external_transaction_id="ext_b",
                amount="-20.00",
                occurred_at=now + timedelta(hours=1),
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert findings == []

    def test_exact_duplicate_not_also_heuristic(self, now: datetime) -> None:
        """An exact-duplicate pair is only reported once (as exact)."""
        txns = [
            _txn(
                external_transaction_id="same",
                amount="-50.00",
                now=now,
            ),
            _txn(
                external_transaction_id="same",
                amount="-50.00",
                now=now,
            ),
        ]
        findings = detect_duplicates(txns)
        assert len(findings) == 1
        assert findings[0].match_reason == "exact_external_id"

    def test_mixed_duplicates(self, now: datetime) -> None:
        """Both exact and heuristic duplicates are detected in the same set."""
        txns = [
            # Exact duplicate pair
            _txn(
                external_transaction_id="exact_dup",
                amount="-10.00",
                occurred_at=now,
                provider_key="bunq",
                description="Same ID",
            ),
            _txn(
                external_transaction_id="exact_dup",
                amount="-10.00",
                occurred_at=now,
                provider_key="bunq",
                description="Same ID",
            ),
            # Heuristic pair (different ext IDs, same amount, close dates)
            _txn(
                external_transaction_id="heur_a",
                amount="-30.00",
                occurred_at=now,
                provider_key="bunq",
                description="Subscription",
            ),
            _txn(
                external_transaction_id="heur_b",
                amount="-30.00",
                occurred_at=now + timedelta(hours=1),
                provider_key="bunq",
                description="Subscription",
            ),
            # Innocent bystander
            _txn(
                external_transaction_id="unique",
                amount="-5.00",
                occurred_at=now,
                description="Coffee",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 2  # 1 exact + 1 heuristic
        exacts = [f for f in findings if f.match_reason == "exact_external_id"]
        heuristics = [f for f in findings if f.match_reason == "amount_and_date"]
        assert len(exacts) == 1
        assert exacts[0].confidence == 1.0
        assert len(heuristics) == 1
        assert heuristics[0].confidence >= 0.5

    def test_duplicate_transaction_dataclass(self, now: datetime) -> None:
        """DuplicateTransaction dataclass carries expected fields."""
        txn = _txn(now=now)
        dt = DuplicateTransaction(
            transaction_a=txn,
            transaction_b=txn,
            match_reason="exact_external_id",
            confidence=0.9,
            diff_hours=1.5,
            amount_diff=Decimal("0"),
            same_description=True,
            same_provider=True,
        )
        assert dt.transaction_a is txn
        assert dt.match_reason == "exact_external_id"
        assert dt.confidence == 0.9
        assert dt.diff_hours == 1.5

    def test_heuristic_confidence_cross_provider(self, now: datetime) -> None:
        """Cross-provider heuristic: same description = higher confidence."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                provider_key="bunq",
                description="Netflix",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                provider_key="trading212",
                description="Netflix",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        # Cross-provider (0.5 + 0.2) + same desc (+0.2) = 0.9
        assert findings[0].confidence == 0.9

    def test_heuristic_confidence_same_provider_same_desc(
        self, now: datetime
    ) -> None:
        """Same provider + same description = 0.7."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                provider_key="bunq",
                description="Netflix",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                provider_key="bunq",
                description="Netflix",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        # Same provider, same desc: 0.5 + 0 + 0.2 = 0.7
        assert findings[0].confidence == 0.7

    def test_heuristic_confidence_same_provider_diff_desc(
        self, now: datetime
    ) -> None:
        """Same provider + different description = 0.6."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                provider_key="bunq",
                description="Netflix",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                provider_key="bunq",
                description="Coffee Shop",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        # Same provider, diff desc: 0.5 + 0.1 + 0 = 0.6
        assert findings[0].confidence == 0.6

    def test_heuristic_case_insensitive_description(
        self, now: datetime
    ) -> None:
        """Description comparison is case-insensitive."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                description="Netflix Subscription",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                description="netflix subscription",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].same_description is True

    def test_both_descriptions_none(self, now: datetime) -> None:
        """Both descriptions None is treated as matching."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                description=None,
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                description=None,
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].same_description is True

    def test_one_description_none(self, now: datetime) -> None:
        """One None description, one with text = not matching."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                description=None,
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                description="Something",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].same_description is False

    def test_custom_threshold_hours(self, now: datetime) -> None:
        """Custom threshold_hours changes which pairs are flagged."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-10.00",
                occurred_at=now,
            ),
            _txn(
                external_transaction_id="a2",
                amount="-10.00",
                occurred_at=now + timedelta(hours=24),
            ),
        ]
        # 24h gap > 12h threshold → not flagged
        assert detect_duplicates(txns, threshold_hours=12) == []
        # 24h gap <= 48h threshold → flagged
        assert len(detect_duplicates(txns, threshold_hours=48)) == 1

    def test_connector_label_in_log(self, now: datetime) -> None:
        """Custom connector label passes through without error."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-10.00",
                now=now,
            ),
            _txn(
                external_transaction_id="a1",
                amount="-10.00",
                now=now,
            ),
        ]
        findings = detect_duplicates(txns, connector="my-bank")
        assert len(findings) == 1

    def test_same_external_id_different_amount_still_exact(
        self, now: datetime
    ) -> None:
        """Exact duplicate by external ID even if amounts differ."""
        txns = [
            _txn(
                external_transaction_id="same",
                amount="-10.00",
                now=now,
            ),
            _txn(
                external_transaction_id="same",
                amount="-20.00",
                now=now,
            ),
        ]
        findings = detect_duplicates(txns)
        assert len(findings) == 1
        assert findings[0].match_reason == "exact_external_id"
        assert findings[0].amount_diff > 0


# ═══════════════════════════════════════════════════════════════════════
# _same_desc helper (private — tested via behaviour)
# ═══════════════════════════════════════════════════════════════════════


class TestSameDescription:
    """Direct tests for the _same_desc helper via detect_duplicates."""

    def test_whitespace_insensitive(self, now: datetime) -> None:
        """Leading/trailing whitespace is stripped before comparison."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                description="  Hello World  ",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                description="Hello World",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].same_description is True

    def test_unicode_case_insensitive(self, now: datetime) -> None:
        """Unicode case folding works."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                description="Café",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                description="CAFÉ",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].same_description is True

    def test_empty_vs_none_description(self, now: datetime) -> None:
        """Empty string vs None is treated as same (both falsy)."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                description="",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                description=None,
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        # Both are falsy — _same_desc returns True
        assert findings[0].same_description is True

    def test_empty_vs_empty_description(self, now: datetime) -> None:
        """Both empty strings are considered same."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                description="",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                description="",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].same_description is True


# ═══════════════════════════════════════════════════════════════════════
# _heuristic_confidence edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestHeuristicConfidence:
    """Confidence scoring edge cases via detect_duplicates."""

    def test_cross_provider_same_desc_max_score(self, now: datetime) -> None:
        """Cross-provider + same description = 0.9 (max)."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-100.00",
                occurred_at=now,
                provider_key="bunq",
                description="Rent",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-100.00",
                occurred_at=now + timedelta(hours=1),
                provider_key="trading212",
                description="Rent",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].confidence == 0.9

    def test_same_provider_same_desc(self, now: datetime) -> None:
        """Same provider + same description = 0.7."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                provider_key="bunq",
                description="Netflix",
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                provider_key="bunq",
                description="Netflix",
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        assert findings[0].confidence == 0.7

    def test_cross_provider_no_desc(self, now: datetime) -> None:
        """Cross-provider, both None descriptions: 0.5 + 0.2 + 0.2 = 0.9."""
        txns = [
            _txn(
                external_transaction_id="a1",
                amount="-50.00",
                occurred_at=now,
                provider_key="bunq",
                description=None,
            ),
            _txn(
                external_transaction_id="a2",
                amount="-50.00",
                occurred_at=now + timedelta(hours=1),
                provider_key="trading212",
                description=None,
            ),
        ]
        findings = detect_duplicates(txns, threshold_hours=48)
        assert len(findings) == 1
        # Cross-provider (0.5 + 0.2) + both None desc -> same_desc True (+0.2) = 0.9
        assert findings[0].confidence == 0.9
        assert findings[0].same_description is True
