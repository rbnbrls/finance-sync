"""Unit tests for the transaction mapper (canonical → Actual Budget format).

These are pure-function tests — no database, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from finance_sync.exporter.transaction_mapper import (
    _as_date,
    _build_imported_id,
    _build_notes,
    _build_payee,
    _cents,
    map_transaction,
    map_transaction_to_csv_row,
)
from finance_sync.models.transaction import Transaction

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_txn(
    *,
    transaction_type: str = "payment",
    amount: Decimal = Decimal("-42.50"),
    currency_code: str = "EUR",
    description: str | None = "Coffee shop",
    status: str = "booked",
    external_transaction_id: str = "txn_ext_001",
    occurred_at: datetime | None = None,
    amount_in_base: Decimal | None = None,
    base_currency_code: str | None = None,
    fx_rate: Decimal | None = None,
    provider_fingerprint: str | None = None,
    security_id: str | None = None,
) -> Transaction:
    """Build a Transaction instance with minimal required fields."""
    return Transaction(
        id=str(uuid4()),
        tenant_id="tenant_001",
        provider_key="bunq",
        external_transaction_id=external_transaction_id,
        account_id="acct_001",
        amount=amount,
        currency_code=currency_code,
        occurred_at=occurred_at or datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
        booked_at=datetime(2025, 6, 15, 14, 0, tzinfo=UTC),
        transaction_type=transaction_type,
        description=description,
        status=status,
        amount_in_base=amount_in_base,
        base_currency_code=base_currency_code,
        fx_rate=fx_rate,
        provider_fingerprint=provider_fingerprint,
        security_id=security_id,
        revision=1,
    )


# ═══════════════════════════════════════════════════════════════════════
# _cents
# ═══════════════════════════════════════════════════════════════════════


class TestCents:
    def test_positive_amount(self) -> None:
        assert _cents(Decimal("12.34")) == 1234

    def test_negative_amount(self) -> None:
        assert _cents(Decimal("-42.50")) == -4250

    def test_zero(self) -> None:
        assert _cents(Decimal("0")) == 0

    def test_large_amount(self) -> None:
        assert _cents(Decimal("1234567.89")) == 123456789

    def test_no_decimal(self) -> None:
        assert _cents(Decimal("100")) == 10000


# ═══════════════════════════════════════════════════════════════════════
# _as_date
# ═══════════════════════════════════════════════════════════════════════


class TestAsDate:
    def test_utc_datetime(self) -> None:
        dt = datetime(2025, 6, 15, 12, 30, tzinfo=UTC)
        assert _as_date(dt).isoformat() == "2025-06-15"

    def test_naive_datetime(self) -> None:
        dt = datetime(2025, 6, 15, 12, 30)
        assert _as_date(dt).isoformat() == "2025-06-15"


# ═══════════════════════════════════════════════════════════════════════
# _build_imported_id
# ═══════════════════════════════════════════════════════════════════════


class TestBuildImportedId:
    def test_uses_fs_prefix(self) -> None:
        txn = _make_txn(external_transaction_id="bunq_98765")
        assert _build_imported_id(txn) == "fs_bunq_98765"

    def test_deterministic(self) -> None:
        txn = _make_txn(external_transaction_id="abc")
        assert _build_imported_id(txn) == _build_imported_id(txn)


# ═══════════════════════════════════════════════════════════════════════
# _build_payee
# ═══════════════════════════════════════════════════════════════════════


class TestBuildPayee:
    def test_uses_description(self) -> None:
        txn = _make_txn(description="Kroger Grocery")
        assert _build_payee(txn, fallback="Imported") == "Kroger Grocery"

    def test_transfer_type(self) -> None:
        txn = _make_txn(transaction_type="transfer", description="Transfer")
        payee = _build_payee(txn, fallback="Imported")
        assert payee == "Transfer"

    def test_no_description_falls_back(self) -> None:
        txn = _make_txn(description=None, transaction_type="dividend")
        assert _build_payee(txn, fallback="Imported") == "Dividend"

    def test_unknown_type_uses_fallback(self) -> None:
        txn = _make_txn(description=None, transaction_type="other")
        assert _build_payee(txn, fallback="Manual entry") == "Manual entry"


# ═══════════════════════════════════════════════════════════════════════
# _build_notes
# ═══════════════════════════════════════════════════════════════════════


class TestBuildNotes:
    def test_basic_transaction_no_fx(self) -> None:
        txn = _make_txn(description="Coffee")
        assert _build_notes(txn) == "Coffee"

    def test_with_fx_conversion(self) -> None:
        txn = _make_txn(
            description="USD Purchase",
            amount=Decimal("-50.00"),
            currency_code="USD",
            amount_in_base=Decimal("-45.50"),
            base_currency_code="EUR",
            fx_rate=Decimal("0.91"),
        )
        notes = _build_notes(txn)
        assert notes is not None
        assert "FX:" in notes
        assert "-50.00 USD" in notes
        assert "-45.50 EUR" in notes
        assert "@ 0.91" in notes

    def test_with_provider_fingerprint(self) -> None:
        txn = _make_txn(
            description="Dividend",
            provider_fingerprint="hash_xyz",
            transaction_type="dividend",
        )
        notes = _build_notes(txn)
        assert notes is not None
        assert "Ref: hash_xyz" in notes

    def test_no_description_returns_none(self) -> None:
        txn = _make_txn(description=None)
        notes = _build_notes(txn)
        assert notes is None or notes == ""


# ═══════════════════════════════════════════════════════════════════════
# map_transaction
# ═══════════════════════════════════════════════════════════════════════


class TestMapTransaction:
    def test_basic_mapping(self) -> None:
        txn = _make_txn(
            external_transaction_id="ext_001",
            description="Amazon Purchase",
            amount=Decimal("-29.99"),
            status="booked",
        )
        mapped = map_transaction(txn, ab_account_name="Checking")

        assert mapped["date"].isoformat() == "2025-06-15"
        assert mapped["account"] == "Checking"
        assert mapped["payee"] == "Amazon Purchase"
        assert mapped["amount"] == -2999  # cents
        assert mapped["imported_id"] == "fs_ext_001"
        assert mapped["cleared"] is True

    def test_uncleared_transaction(self) -> None:
        txn = _make_txn(status="pending")
        mapped = map_transaction(txn, ab_account_name="Checking")
        assert mapped["cleared"] is False

    def test_positive_amount_inflow(self) -> None:
        txn = _make_txn(amount=Decimal("1500.00"), description="Salary")
        mapped = map_transaction(txn, ab_account_name="Checking")
        assert mapped["amount"] == 150000  # cents, positive

    def test_imported_payee_uses_description(self) -> None:
        txn = _make_txn(description="ALDI MUNICH")
        mapped = map_transaction(txn, ab_account_name="Checking")
        assert mapped["imported_payee"] == "ALDI MUNICH"


# ═══════════════════════════════════════════════════════════════════════
# map_transaction_to_csv_row
# ═══════════════════════════════════════════════════════════════════════


class TestMapTransactionToCsvRow:
    def test_basic_csv_row(self) -> None:
        txn = _make_txn(
            description="Supermarkt",
            amount=Decimal("-85.30"),
        )
        row = map_transaction_to_csv_row(txn)
        assert row["Date"] == "2025-06-15"
        assert row["Payee"] == "Supermarkt"
        assert row["Amount"] == "-85.30"

    def test_csv_with_fx_notes(self) -> None:
        txn = _make_txn(
            description="USD Purchase",
            amount=Decimal("-50.00"),
            amount_in_base=Decimal("-45.50"),
            base_currency_code="EUR",
            fx_rate=Decimal("0.91"),
        )
        row = map_transaction_to_csv_row(txn)
        assert "FX:" in row["Notes"]
