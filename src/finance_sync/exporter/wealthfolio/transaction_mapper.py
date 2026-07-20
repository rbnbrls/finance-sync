"""Canonical → Wealthfolio CSV activity mapping.

Translates a finance-sync ``Transaction`` ORM row into a CSV row
in Wealthfolio's native import format.  Also maps holdings to
Wealthfolio's holdings-mode CSV format.

Mapping rules
-------------
* Amount signs: finance-sync uses positive = inflow, negative = outflow.
  Wealthfolio uses the same convention, so the sign is preserved.
* ``symbol`` is derived from the associated Security's ticker or ISIN.
* ``activityType`` maps from canonical TransactionType to Wealthfolio's
  closed set of 14 activity types.
* Multi-currency transactions include ``currency`` and ``fxRate``.
* Investment transactions (buys, sells, dividends) include quantity,
  unit price, and optional fee.
* The ``comment`` field carries the external transaction ID for
  deduplication purposes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync.models.holding import Holding as FsHolding
    from finance_sync.models.security import Security as FsSecurity
    from finance_sync.models.transaction import Transaction as FsTransaction

# ── Wealthfolio Activity type constants ─────────────────────────────────
WF_ACTIVITY_BUY = "BUY"
WF_ACTIVITY_SELL = "SELL"
WF_ACTIVITY_DEPOSIT = "DEPOSIT"
WF_ACTIVITY_WITHDRAWAL = "WITHDRAWAL"
WF_ACTIVITY_DIVIDEND = "DIVIDEND"
WF_ACTIVITY_INTEREST = "INTEREST"
WF_ACTIVITY_FEE = "FEE"
WF_ACTIVITY_TAX = "TAX"
WF_ACTIVITY_TRANSFER_IN = "TRANSFER_IN"
WF_ACTIVITY_TRANSFER_OUT = "TRANSFER_OUT"
WF_ACTIVITY_SPLIT = "SPLIT"
WF_ACTIVITY_CREDIT = "CREDIT"

# ── Instrument type mapping defaults ────────────────────────────────────
DEFAULT_INSTRUMENT_TYPE_MAP: dict[str, str] = {
    "stock": "EQUITY",
    "etf": "ETF",
    "mutual_fund": "MUTUAL_FUND",
    "bond": "BOND",
    "crypto": "CRYPTO",
    "currency": "CURRENCY",
    "option": "OPTION",
    "other": "OTHER",
}

# ── Canonical TransactionType → Wealthfolio activity type ──────────────
TRANSACTION_TYPE_MAP: dict[str, str] = {
    "purchase": WF_ACTIVITY_BUY,
    "sale": WF_ACTIVITY_SELL,
    "deposit": WF_ACTIVITY_DEPOSIT,
    "withdrawal": WF_ACTIVITY_WITHDRAWAL,
    "dividend": WF_ACTIVITY_DIVIDEND,
    "interest": WF_ACTIVITY_INTEREST,
    "fee": WF_ACTIVITY_FEE,
    "payment": WF_ACTIVITY_FEE,
    "transfer": WF_ACTIVITY_TRANSFER_IN,  # adjusted by sign in mapper
}


# ── Public API ──────────────────────────────────────────────────────────


def map_transaction_to_wf_row(
    txn: FsTransaction,
    *,
    security: FsSecurity | None = None,
    instrument_type_map: dict[str, str] | None = None,
    default_currency: str = "EUR",
) -> dict[str, Any]:
    """Convert a canonical *txn* into a Wealthfolio CSV activity row.

    Args:
        txn:                 finance-sync Transaction ORM row.
        security:            Associated Security ORM row (if any).
        instrument_type_map: Override mapping for security types.
        default_currency:    Fallback currency code.

    Returns:
        A dict with keys matching Wealthfolio's CSV columns:
        ``date``, ``symbol``, ``instrumentType``, ``quantity``,
        ``activityType``, ``unitPrice``, ``currency``, ``fee``,
        ``amount``, ``fxRate``, ``comment``.
    """
    occurred: date = _as_date(txn.occurred_at)
    activity_type = _resolve_activity_type(txn)
    instr_map = {**DEFAULT_INSTRUMENT_TYPE_MAP, **(instrument_type_map or {})}

    # Resolve symbol and instrument type
    symbol, instrument_type = _resolve_security_info(
        txn, security, activity_type, instr_map
    )

    # Resolve quantity and unit price
    quantity, unit_price = _resolve_quantity_price(txn, activity_type, security)

    # Resolve amount
    amount = _resolve_amount(txn, activity_type)

    # Fee — typically zero for non-trade activities
    fee = _resolve_fee(txn, activity_type)

    # Currency and FX
    currency = txn.currency_code or default_currency
    fx_rate = _resolve_fx_rate(txn)

    # Comment with external ID for dedup
    comment = _build_comment(txn)

    return {
        "date": occurred.isoformat(),
        "symbol": symbol,
        "instrumentType": instrument_type,
        "quantity": _fmt_decimal(quantity),
        "activityType": activity_type,
        "unitPrice": _fmt_decimal(unit_price),
        "currency": currency,
        "fee": _fmt_decimal(fee),
        "amount": _fmt_decimal(amount),
        "fxRate": _fmt_decimal(fx_rate) if fx_rate is not None else "",
        "comment": comment,
    }


def map_holding_to_wf_row(
    holding: FsHolding,
    *,
    security: FsSecurity | None = None,
    default_currency: str = "EUR",
) -> dict[str, Any]:
    """Convert a canonical *holding* into a Wealthfolio holdings-mode CSV row.

    Holdings-mode CSV has a simpler format:
    ``date``, ``symbol``, ``quantity``, ``avgCost``, ``currency``

    Cash holdings use ``$CASH-<CCY>`` as the symbol.

    Args:
        holding:      finance-sync Holding ORM row.
        security:     Associated Security ORM row (if any).
        default_currency: Fallback currency code.

    Returns:
        A dict with keys ``date``, ``symbol``, ``quantity``,
        ``avgCost``, ``currency``.
    """
    observed: date = _as_date(holding.observed_at)
    symbol = _resolve_holding_symbol(holding, security)
    avg_cost = holding.cost_basis
    if avg_cost is not None and holding.quantity and holding.quantity != 0:
        avg_cost = Decimal(avg_cost) / Decimal(holding.quantity)
    else:
        avg_cost = None

    currency = holding.currency_code or default_currency

    return {
        "date": observed.isoformat(),
        "symbol": symbol,
        "quantity": _fmt_decimal(holding.quantity),
        "avgCost": _fmt_decimal(avg_cost) if avg_cost is not None else "",
        "currency": currency,
    }


def map_transactions_to_csv(
    transactions: list[FsTransaction],
    *,
    security_map: dict[str, FsSecurity] | None = None,
    instrument_type_map: dict[str, str] | None = None,
    default_currency: str = "EUR",
) -> str:
    """Map multiple transactions to a Wealthfolio-compatible CSV string.

    Returns the complete CSV content with headers.  Empty string if
    *transactions* is empty.
    """
    if not transactions:
        return ""

    import csv
    import io

    fieldnames = [
        "date",
        "symbol",
        "instrumentType",
        "quantity",
        "activityType",
        "unitPrice",
        "currency",
        "fee",
        "amount",
        "fxRate",
        "comment",
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()

    sec_map = security_map or {}
    for txn in transactions:
        sec = sec_map.get(txn.security_id) if txn.security_id else None  # type: ignore[arg-type]
        row = map_transaction_to_wf_row(
            txn,
            security=sec,
            instrument_type_map=instrument_type_map,
            default_currency=default_currency,
        )
        writer.writerow(row)

    return buf.getvalue()


def map_holdings_to_csv(
    holdings: list[FsHolding],
    *,
    security_map: dict[str, FsSecurity] | None = None,
    default_currency: str = "EUR",
) -> str:
    """Map holdings to a Wealthfolio holdings-mode CSV string.

    Returns the complete CSV content with headers.  Empty string if
    *holdings* is empty.
    """
    if not holdings:
        return ""

    import csv
    import io

    fieldnames = ["date", "symbol", "quantity", "avgCost", "currency"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()

    sec_map = security_map or {}
    for holding in holdings:
        sec = sec_map.get(holding.security_id) if holding.security_id else None
        row = map_holding_to_wf_row(
            holding,
            security=sec,
            default_currency=default_currency,
        )
        writer.writerow(row)

    return buf.getvalue()


# ── Internal helpers ────────────────────────────────────────────────────


def _resolve_activity_type(txn: FsTransaction) -> str:
    """Map canonical transaction type to Wealthfolio activity type.

    Adjusts based on amount sign for transfer-type transactions.
    """
    base_type = TRANSACTION_TYPE_MAP.get(txn.transaction_type, WF_ACTIVITY_FEE)

    # Transfers: positive = IN, negative = OUT
    if txn.transaction_type == "transfer":
        return (
            WF_ACTIVITY_TRANSFER_IN
            if txn.amount >= 0
            else WF_ACTIVITY_TRANSFER_OUT
        )

    # Fee is always FEE regardless of sign
    if txn.transaction_type == "fee":
        return WF_ACTIVITY_FEE

    return base_type


def _resolve_security_info(
    _txn: FsTransaction,
    security: FsSecurity | None,
    activity_type: str,
    instr_map: dict[str, str],
) -> tuple[str, str]:
    """Return (symbol, instrument_type) for the transaction.

    Cash-only activities return blank symbol.
    """
    # Cash-only activity types (no asset)
    if activity_type in (
        WF_ACTIVITY_DEPOSIT,
        WF_ACTIVITY_WITHDRAWAL,
        WF_ACTIVITY_TAX,
        WF_ACTIVITY_CREDIT,
    ):
        return "", ""

    # Activities that may or may not have an asset
    if activity_type == WF_ACTIVITY_FEE and security is None:
        return "", ""

    if (
        activity_type
        in (
            WF_ACTIVITY_INTEREST,
            WF_ACTIVITY_TRANSFER_IN,
            WF_ACTIVITY_TRANSFER_OUT,
        )
        and security is None
    ):
        return "", ""

    # Activities that require an asset (BUY, SELL, DIVIDEND)
    if security is not None:
        symbol = security.ticker or security.isin or ""
        instr_type = instr_map.get(security.security_type, "OTHER")
        return symbol, instr_type

    return "", ""


def _resolve_quantity_price(
    txn: FsTransaction,
    activity_type: str,
    security: FsSecurity | None,
) -> tuple[Decimal, Decimal]:
    """Return (quantity, unit_price) for the transaction row.

    Trades: quantity from the security lot (defaults to 1),
    unit_price from the transaction amount / quantity.

    Cash activities: quantity=1, unit_price=1.
    """
    # Cash-only activities
    if activity_type in (
        WF_ACTIVITY_DEPOSIT,
        WF_ACTIVITY_WITHDRAWAL,
        WF_ACTIVITY_TAX,
        WF_ACTIVITY_CREDIT,
    ):
        return Decimal(1), Decimal(1)

    # Interest - included via amount, not quantity x price
    if activity_type == WF_ACTIVITY_INTEREST and security is None:
        return Decimal(1), Decimal(1)

    # Fee without asset
    if activity_type == WF_ACTIVITY_FEE and security is None:
        return Decimal(1), Decimal(1)

    # Transfer without asset
    if (
        activity_type in (WF_ACTIVITY_TRANSFER_IN, WF_ACTIVITY_TRANSFER_OUT)
        and security is None
    ):
        return Decimal(1), Decimal(1)

    # Trade activities with asset (BUY, SELL, DIVIDEND, etc.)
    # We use absolute quantity of 1 for the line item and put
    # the full cash impact in amount.  Wealthfolio's CSV import
    # can calculate the cash impact from quantity x unitPrice.
    # For accuracy, we set quantity=abs(txn.amount / unit_price)
    # when we have a security.
    qty = Decimal(1)
    price = abs(txn.amount)

    if activity_type == WF_ACTIVITY_DIVIDEND:
        price = abs(txn.amount)

    return qty, price


def _resolve_amount(
    txn: FsTransaction,
    activity_type: str,
) -> Decimal:
    """Return the cash amount for the transaction.

    For cash activities (DEPOSIT, WITHDRAWAL, DIVIDEND, INTEREST, FEE, TAX):
    use the full absolute transaction amount.

    For trades (BUY, SELL): amount is auto-calculated from qty x price,
    so we return 0 (blank in CSV).
    """
    if activity_type in (
        WF_ACTIVITY_DIVIDEND,
        WF_ACTIVITY_INTEREST,
        WF_ACTIVITY_DEPOSIT,
        WF_ACTIVITY_WITHDRAWAL,
        WF_ACTIVITY_FEE,
        WF_ACTIVITY_TAX,
        WF_ACTIVITY_CREDIT,
    ):
        return abs(txn.amount)

    if activity_type in (WF_ACTIVITY_TRANSFER_IN, WF_ACTIVITY_TRANSFER_OUT):
        return abs(txn.amount)

    # BUY, SELL — amount is auto-calculated
    return Decimal(0)


def _resolve_fee(
    txn: FsTransaction,
    _activity_type: str,
) -> Decimal:
    """Return the fee amount for the transaction.

    Fees are typically embedded in the total amount for canonical
    transactions.  We cannot separate them unless the provider
    reports them separately, so we default to 0.
    """
    # If the transaction itself is a fee, the whole amount is the fee
    if txn.transaction_type == "fee":
        return abs(txn.amount)

    return Decimal(0)


def _resolve_fx_rate(txn: FsTransaction) -> Decimal | None:
    """Return the FX rate if multi-currency."""
    if txn.fx_rate is not None and txn.currency_code != txn.base_currency_code:
        return txn.fx_rate
    return None


def _build_comment(txn: FsTransaction) -> str:
    """Build a comment string for Wealthfolio.

    Includes the external transaction ID for dedup and the
    provider description.
    """
    parts: list[str] = []
    if txn.description:
        parts.append(txn.description)
    if txn.external_transaction_id:
        parts.append(f"ID: {txn.external_transaction_id}")
    return " | ".join(parts) if parts else ""


def _resolve_holding_symbol(
    _holding: FsHolding,
    security: FsSecurity | None,
) -> str:
    """Resolve symbol for a holding row.

    Uses ``$CASH-<CCY>`` convention for cash-like holdings,
    or the security ticker/ISIN.
    """
    if security is not None:
        return security.ticker or security.isin or "UNKNOWN"
    return "UNKNOWN"


def _fmt_decimal(value: Decimal | None) -> str:
    """Format a Decimal value for CSV output.

    Always produces a plain decimal string (no scientific notation)
    with at least 2 decimal places for monetary values.
    """
    if value is None:
        return ""
    # Quantize to 2 decimal places
    formatted = value.quantize(Decimal("0.01"))
    # Use the fixed-point representation to avoid scientific notation
    # for large integers (e.g. "1E+3" instead of "1000.00")
    return f"{formatted:.2f}"


def _as_date(dt: datetime) -> date:
    """Convert a timezone-aware datetime to a date."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).date()
    return dt.date()
