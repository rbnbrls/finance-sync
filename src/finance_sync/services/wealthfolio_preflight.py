"""Pre-export data contract for the Wealthfolio projection.

The preflight is deliberately independent from SQLAlchemy and the
Wealthfolio client.  The data-health view and the exporter can therefore use
the exact same rules without making the exporter depend on UI code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date


@dataclass(frozen=True, slots=True)
class PreflightFinding:
    """A deterministic finding attached to one canonical record."""

    category: str
    severity: str
    record_id: str
    message: str


@dataclass(slots=True)
class WealthfolioPreflightResult:
    """Validation result and records safe to project downstream."""

    exportable_holdings: list[Any] = field(default_factory=list)
    quarantined_holdings: list[Any] = field(default_factory=list)
    findings: list[PreflightFinding] = field(default_factory=list)

    @property
    def blocking_findings(self) -> list[PreflightFinding]:
        return [item for item in self.findings if item.severity == "error"]


def validate_holdings(holdings: list[Any]) -> WealthfolioPreflightResult:
    """Validate holdings before they become Wealthfolio valuations.

    A non-zero position without either a market value or unit price cannot be
    represented as a reliable valuation.  It is quarantined instead of being
    sent downstream as an implicit zero.  Missing cost basis is a warning:
    Wealthfolio can still display the position, but gain/loss is degraded.
    """

    result = WealthfolioPreflightResult()
    for holding in holdings:
        quantity = _decimal(getattr(holding, "quantity", None))
        market_value = _decimal(getattr(holding, "market_value", None))
        price = _decimal(getattr(holding, "price", None))
        record_id = str(getattr(holding, "id", "unknown"))

        if quantity != 0 and market_value is None and price is None:
            result.quarantined_holdings.append(holding)
            result.findings.append(
                PreflightFinding(
                    category="incomplete_valuation",
                    severity="error",
                    record_id=record_id,
                    message=(
                        "non-zero holding has neither market_value nor price"
                    ),
                )
            )
            continue

        result.exportable_holdings.append(holding)
        if quantity != 0 and getattr(holding, "cost_basis", None) is None:
            result.findings.append(
                PreflightFinding(
                    category="incomplete_cost_basis",
                    severity="warning",
                    record_id=record_id,
                    message="non-zero holding has no cost basis",
                )
            )

    return result


def validate_transaction_stream(
    transactions: list[Any],
) -> list[PreflightFinding]:
    """Validate trade fields and pairable transfer metadata.

    This does not invent missing transfer legs.  A provider transfer id is
    treated as the authoritative pairing key; when it is absent, the
    canonical counterparty reference is the fallback key.  Pairing is scoped
    to account, currency and calendar date to avoid false matches.
    """

    findings: list[PreflightFinding] = []
    transfers: dict[tuple[str, str, date, str], list[Any]] = {}
    for txn in transactions:
        txn_type = str(getattr(txn, "transaction_type", ""))
        record_id = str(getattr(txn, "id", "unknown"))
        if txn_type in {"purchase", "sale"} and (
            getattr(txn, "quantity", None) is None
            or getattr(txn, "unit_price", None) is None
        ):
            findings.append(
                PreflightFinding(
                    category="incomplete_transaction",
                    severity="error",
                    record_id=record_id,
                    message="trade is missing quantity or unit price",
                )
            )
        if txn_type != "transfer":
            continue
        raw_metadata = getattr(txn, "provider_metadata_contract", None)
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        provider_id = _first_text(
            metadata,
            "transfer_id",
            "transferId",
            "transfer_reference",
            "transferReference",
        )
        fallback = str(
            getattr(txn, "counterparty_account_reference", None) or ""
        )
        pairing_key = provider_id or fallback
        if pairing_key:
            key = (
                str(getattr(txn, "currency_code", "")),
                pairing_key,
                txn.occurred_at.date(),
                str(abs(_decimal(getattr(txn, "amount", None)) or Decimal(0))),
            )
            transfers.setdefault(key, []).append(txn)
        else:
            findings.append(
                PreflightFinding(
                    category="unbalanced_transfer",
                    severity="warning",
                    record_id=record_id,
                    message=(
                        "transfer has no provider or counterparty reference"
                    ),
                )
            )

    for rows in transfers.values():
        amounts = [
            _decimal(getattr(row, "amount", None)) or Decimal(0) for row in rows
        ]
        if len(rows) < 2 or not any(value > 0 for value in amounts) or not any(
            value < 0 for value in amounts
        ):
            findings.extend(
                PreflightFinding(
                    category="unbalanced_transfer",
                    severity="warning",
                    record_id=str(getattr(row, "id", "unknown")),
                    message="transfer has no matched opposite-signed leg",
                )
                for row in rows
            )
    return findings


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value)
    return None
