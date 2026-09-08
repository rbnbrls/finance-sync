"""Pre-export data contract for the Wealthfolio projection.

The preflight is deliberately independent from SQLAlchemy and the
Wealthfolio client.  The data-health view and the exporter can therefore use
the exact same rules without making the exporter depend on UI code.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date


def _any_list() -> list[Any]:
    return []


def _finding_list() -> list[PreflightFinding]:
    return []


def _empty_destination_activities() -> dict[
    str, tuple[dict[str, Any], ...]
]:
    return {}


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

    exportable_holdings: list[Any] = field(default_factory=_any_list)
    quarantined_holdings: list[Any] = field(default_factory=_any_list)
    findings: list[PreflightFinding] = field(default_factory=_finding_list)

    @property
    def blocking_findings(self) -> list[PreflightFinding]:
        return [item for item in self.findings if item.severity == "error"]


@dataclass(frozen=True, slots=True)
class WealthfolioDestinationProbe:
    """Bounded, sanitized result of an opt-in Wealthfolio probe."""

    status: str
    reason: str | None = None
    accounts: tuple[dict[str, Any], ...] = ()
    assets: tuple[dict[str, Any], ...] = ()
    activities: dict[str, tuple[dict[str, Any], ...]] = field(
        default_factory=_empty_destination_activities
    )


def missing_wealthfolio_assets(
    canonical_assets: list[Any],
    remote_assets: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    """Return canonical security IDs absent from the remote asset catalog."""
    remote_keys = {
        str(value).strip().upper()
        for asset in remote_assets
        for value in (asset.get("isin"), asset.get("symbol"))
        if value
    }
    missing: list[str] = []
    for asset in canonical_assets:
        identity = str(
            getattr(asset, "isin", None) or getattr(asset, "ticker", None) or ""
        ).strip().upper()
        if identity and identity not in remote_keys:
            missing.append(str(getattr(asset, "id", "unknown")))
    return tuple(sorted(set(missing)))


async def probe_wealthfolio_destination(
    client: Any,
    *,
    timeout_seconds: float = 5.0,
    include_activities: bool = False,
) -> WealthfolioDestinationProbe:
    """Authenticate and read remote parity inputs within a hard timeout.

    The caller owns client construction and secret handling.  This helper does
    not log or return credentials; callers should pass its result to a
    tenant-scoped parity comparison and expose only counts/identities.
    """
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioAuthError,
        WealthfolioClientError,
    )

    try:
        async with asyncio.timeout(timeout_seconds):
            authenticated = await client.authenticate()
            if not authenticated:
                return WealthfolioDestinationProbe(
                    status="unauthorized", reason="authentication rejected"
                )
            accounts = tuple(await client.get_accounts())
            assets = tuple(await client.get_assets())
            activities: dict[str, tuple[dict[str, Any], ...]] = {}
            if include_activities:
                for account in accounts:
                    remote_id = str(account.get("id") or "").strip()
                    if not remote_id:
                        continue
                    rows = await client.get_all_activities(remote_id)
                    activities[remote_id] = tuple(rows)
            return WealthfolioDestinationProbe(
                status="ready",
                accounts=accounts,
                assets=assets,
                activities=activities,
            )
    except WealthfolioAuthError:
        return WealthfolioDestinationProbe(
            status="unauthorized", reason="authentication failed"
        )
    except TimeoutError:
        return WealthfolioDestinationProbe(
            status="unavailable", reason="probe timeout"
        )
    except WealthfolioClientError:
        return WealthfolioDestinationProbe(
            status="unavailable", reason="destination request failed"
        )


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
        findings.extend(validate_activity_semantics(txn))
        if txn_type != "transfer":
            continue
        raw_metadata = getattr(txn, "provider_metadata_contract", None)
        metadata: dict[str, Any] = (
            cast("dict[str, Any]", raw_metadata)
            if isinstance(raw_metadata, dict)
            else {}
        )
        metadata_candidates: list[dict[str, Any]] = [metadata]
        fields = metadata.get("fields")
        if isinstance(fields, dict):
            metadata_candidates.append(cast("dict[str, Any]", fields))
        provider_id = next(
            (
                value
                for candidate in metadata_candidates
                if (
                    value := _first_text(
                        candidate,
                        "transfer_id",
                        "transferId",
                        "transfer_reference",
                        "transferReference",
                    )
                )
            ),
            None,
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
        if (
            len(rows) < 2
            or not any(value > 0 for value in amounts)
            or not any(value < 0 for value in amounts)
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


def validate_transfer_rows(
    rows: Iterable[Iterable[Any]],
) -> list[PreflightFinding]:
    """Apply the shared transfer rules to lightweight query rows.

    Data health uses an aggregate query for its normal read path.  This
    adapter keeps that query contract small while routing transfer semantics
    through the same validator used by the Wealthfolio exporter.
    """
    normalized_rows: list[tuple[int, Any, Any, Any, Any, Any, Any]] = []
    for index, row in enumerate(rows):
        values = tuple(row)
        if len(values) >= 7:
            (
                record_id,
                _account_id,
                _name,
                amount,
                currency,
                occurred,
                description,
            ) = values[:7]
        else:
            if len(values) < 6:
                continue
            _account_id = values[0]
            _name = values[1]
            amount = values[2]
            currency = values[3]
            occurred = values[4]
            description = values[5]
            record_id = f"transfer-row:{index}"
        occurred_date = (
            occurred.date() if hasattr(occurred, "date") else occurred
        )
        normalized_rows.append(
            (
                index,
                record_id,
                amount,
                currency,
                occurred_date,
                description,
                occurred,
            )
        )

    pair_keys: dict[int, str] = {}
    for position, row in enumerate(normalized_rows):
        (
            index,
            _record_id,
            amount,
            currency,
            occurred_date,
            _description,
            _,
        ) = row
        if index in pair_keys:
            continue
        amount_decimal = _decimal(amount)
        if amount_decimal in (None, Decimal(0)):
            continue
        for candidate in normalized_rows[position + 1 :]:
            candidate_index = candidate[0]
            if candidate_index in pair_keys:
                continue
            candidate_amount = _decimal(candidate[2])
            if (
                candidate_amount is None
                or candidate_amount == 0
                or candidate_amount * amount_decimal >= 0
                or str(candidate[3]) != str(currency)
                or candidate[4] != occurred_date
                or abs(candidate_amount) != abs(amount_decimal)
            ):
                continue
            pair_key = f"query-transfer:{min(index, candidate_index)}"
            pair_keys[index] = pair_key
            pair_keys[candidate_index] = pair_key
            break

    transactions: list[Any] = []
    for (
        index,
        record_id,
        amount,
        currency,
        _occurred_date,
        description,
        occurred,
    ) in normalized_rows:
        if hasattr(occurred, "date"):
            occurred_at = datetime.combine(
                occurred.date(), time.min, tzinfo=UTC
            )
        else:
            occurred_at = datetime.combine(occurred, time.min, tzinfo=UTC)
        transactions.append(
            type(
                "TransferRow",
                (),
                {
                    "id": record_id,
                    "transaction_type": "transfer",
                    "amount": amount,
                    "currency_code": currency,
                    "occurred_at": occurred_at,
                    "description": description,
                    "provider_metadata_contract": {},
                    "counterparty_account_reference": pair_keys.get(index),
                },
            )()
        )
    return [
        finding
        for finding in validate_transaction_stream(transactions)
        if finding.category
        in {"unbalanced_transfer", "incomplete_transaction"}
    ]


def validate_activity_semantics(txn: Any) -> list[PreflightFinding]:
    """Apply activity rules shared by Data health and Wealthfolio export."""
    txn_type = str(getattr(txn, "transaction_type", ""))
    record_id = str(getattr(txn, "id", "unknown"))
    findings: list[PreflightFinding] = []

    if hasattr(txn, "external_transaction_id") and not str(
        txn.external_transaction_id or ""
    ).strip():
        findings.append(
            PreflightFinding(
                category="invalid_activity_semantics",
                severity="error",
                record_id=record_id,
                message="activity has no stable external transaction ID",
            )
        )

    if txn_type in {"purchase", "sale"}:
        missing_trade_fields: list[str] = []
        if getattr(txn, "quantity", None) is None:
            missing_trade_fields.append("quantity")
        if getattr(txn, "unit_price", None) is None:
            missing_trade_fields.append("unit price")
        if hasattr(txn, "security_id") and txn.security_id is None:
            missing_trade_fields.append("security")
        if missing_trade_fields:
            findings.append(
                PreflightFinding(
                    category="incomplete_transaction",
                    severity="error",
                    record_id=record_id,
                    message=(
                        "trade is missing " + ", ".join(missing_trade_fields)
                    ),
                )
            )
        quantity = _decimal(getattr(txn, "quantity", None))
        if (
            getattr(txn, "quantity", None) is not None
            and (quantity is None or quantity <= 0)
        ):
            findings.append(
                PreflightFinding(
                    category="invalid_activity_semantics",
                    severity="error",
                    record_id=record_id,
                    message="trade has a non-positive or invalid quantity",
                )
            )
        unit_price = _decimal(getattr(txn, "unit_price", None))
        if (
            getattr(txn, "unit_price", None) is not None
            and (unit_price is None or unit_price < 0)
        ):
            findings.append(
                PreflightFinding(
                    category="invalid_activity_semantics",
                    severity="error",
                    record_id=record_id,
                    message="trade has a negative or invalid unit price",
                )
            )

    if hasattr(txn, "currency_code"):
        currency_code = str(txn.currency_code or "")
        if not _is_currency_code(currency_code):
            findings.append(
                PreflightFinding(
                    category="invalid_activity_semantics",
                    severity="error",
                    record_id=record_id,
                    message="activity has an invalid ISO-4217 currency code",
                )
            )

    if (
        txn_type in {"purchase", "sale"}
        and _has_explicit_attribute(txn, "security_currency_code")
        and _has_explicit_attribute(txn, "currency_code")
    ):
        security_currency = str(txn.security_currency_code or "").upper()
        activity_currency = str(txn.currency_code or "").upper()
        fx_rate = _decimal(getattr(txn, "fx_rate", None))
        if (
            security_currency
            and activity_currency
            and security_currency != activity_currency
            and (fx_rate is None or fx_rate <= 0)
        ):
            findings.append(
                PreflightFinding(
                    category="invalid_activity_semantics",
                    severity="warning",
                    record_id=record_id,
                    message=(
                        "trade is missing a positive FX rate for the "
                        "security currency conversion"
                    ),
                )
            )

    if (
        txn_type
        in {
            "deposit",
            "withdrawal",
            "dividend",
            "interest",
            "fee",
            "tax",
        }
        and hasattr(txn, "amount")
        and txn.amount is None
    ):
        findings.append(
            PreflightFinding(
                category="incomplete_transaction",
                severity="error",
                record_id=record_id,
                message="cash activity is missing amount",
            )
        )

    transfer_amount = _decimal(getattr(txn, "amount", None))
    if (
        txn_type == "transfer"
        and hasattr(txn, "amount")
        and (
            getattr(txn, "amount", None) is None
            or transfer_amount is None
            or transfer_amount == Decimal(0)
        )
    ):
        findings.append(
            PreflightFinding(
                category="incomplete_transaction",
                severity="error",
                record_id=record_id,
                message="transfer is missing a non-zero amount or direction",
            )
        )

    if (
        txn_type == "transfer"
        and getattr(txn, "security_id", None) is not None
        and getattr(txn, "quantity", None) is None
    ):
        findings.append(
            PreflightFinding(
                category="incomplete_transaction",
                severity="error",
                record_id=record_id,
                message="security transfer is missing quantity",
            )
        )

    fee_amount = getattr(txn, "fee_amount", None)
    if txn_type in {"fee", "tax"} and fee_amount is not None:
        if (_decimal(fee_amount) or Decimal(0)) <= 0:
            findings.append(
                PreflightFinding(
                    category="invalid_activity_semantics",
                    severity="error",
                    record_id=record_id,
                    message=(
                        "fee or tax activity must have a positive fee amount"
                    ),
                )
            )
        fee_currency = str(
            getattr(txn, "fee_currency_code", None)
            or getattr(txn, "currency_code", "")
            or ""
        )
        if not _is_currency_code(fee_currency):
            findings.append(
                PreflightFinding(
                    category="invalid_activity_semantics",
                    severity="error",
                    record_id=record_id,
                    message="fee or tax activity has an invalid currency code",
                )
            )

    unit_price = _decimal(getattr(txn, "unit_price", None))
    amount = _decimal(getattr(txn, "amount", None))
    quantity = getattr(txn, "quantity", None)
    if txn_type in {"purchase", "sale"} and (
        unit_price == 0 or (quantity is not None and amount == 0)
    ):
        findings.append(
            PreflightFinding(
                category="zero_cost_transaction",
                severity="warning",
                record_id=record_id,
                message="trade has zero cost and may be a corporate action",
            )
        )

    if (
        txn_type in {"split", "adjustment", "corporate_action"}
        and quantity_event_ratio(
            getattr(txn, "provider_metadata_contract", None)
        )
        is None
    ):
        findings.append(
            PreflightFinding(
                category="invalid_activity_semantics",
                severity="warning",
                record_id=record_id,
                message="quantity event is missing a positive split ratio",
            )
        )

    occurred_at = getattr(txn, "occurred_at", None)
    booked_at = getattr(txn, "booked_at", None)
    if (
        occurred_at is not None
        and booked_at is not None
        and booked_at < occurred_at
    ):
        findings.append(
            PreflightFinding(
                category="invalid_activity_semantics",
                severity="error",
                record_id=record_id,
                message="booked_at precedes occurred_at",
            )
        )
    return findings


def _is_currency_code(value: str) -> bool:
    """Check the canonical ISO-4217 shape without adding a lookup dependency."""
    return (
        len(value) == 3
        and value.isascii()
        and value.isalpha()
        and value.isupper()
    )


def _has_explicit_attribute(value: Any, name: str) -> bool:
    """Avoid treating dynamically generated mock attributes as real fields."""
    namespace = getattr(value, "__dict__", {})
    return isinstance(namespace, dict) and name in namespace


def quantity_event_ratio(metadata: object) -> Decimal | None:
    """Return an explicit new-units/old-units ratio from safe metadata.

    Connector metadata is persisted as the versioned ``ProviderMetadata``
    contract, whose provider fields live below ``fields``.  Accept the
    contract envelope and the legacy flat shape, but keep the key allowlist
    explicit so arbitrary provider payloads cannot influence reconciliation.
    """
    if not isinstance(metadata, dict):
        return None
    typed_metadata = cast("dict[str, object]", metadata)
    candidates: list[dict[str, object]] = [typed_metadata]
    fields = typed_metadata.get("fields")
    if isinstance(fields, dict):
        candidates.append(cast("dict[str, object]", fields))
    # Some connectors retain a small event object inside the contract.  Only
    # these known containers are traversed; arbitrary nested provider data is
    # deliberately ignored.
    for candidate in tuple(candidates):
        for container_key in (
            "event",
            "corporate_action",
            "corporateAction",
            "action",
        ):
            nested = candidate.get(container_key)
            if isinstance(nested, dict):
                candidates.append(cast("dict[str, object]", nested))
    ratio_keys = (
        "split_ratio",
        "splitRatio",
        "quantity_multiplier",
        "quantityMultiplier",
        "ratio",
    )
    for candidate in candidates:
        for key in ratio_keys:
            value = candidate.get(key)
            if value is None:
                continue
            return _positive_ratio(value)
        numerator = candidate.get("split_numerator")
        denominator = candidate.get("split_denominator")
        if numerator is None:
            numerator = candidate.get("new_quantity")
        if numerator is None:
            numerator = candidate.get("newQuantity")
        if numerator is None:
            numerator = candidate.get("new_units")
        if numerator is None:
            numerator = candidate.get("newUnits")
        if denominator is None:
            denominator = candidate.get("old_quantity")
        if denominator is None:
            denominator = candidate.get("oldQuantity")
        if denominator is None:
            denominator = candidate.get("old_units")
        if denominator is None:
            denominator = candidate.get("oldUnits")
        if numerator is not None and denominator is not None:
            try:
                numerator_decimal = Decimal(str(numerator))
                denominator_decimal = Decimal(str(denominator))
                if denominator_decimal > 0:
                    ratio = numerator_decimal / denominator_decimal
                    return ratio if ratio > 0 else None
            except Exception:
                return None
    return None


def _positive_ratio(value: object) -> Decimal | None:
    """Parse numeric or explicit provider ratio notation safely."""
    normalized = str(value).strip().casefold().replace(",", ".")
    ratio_match = re.fullmatch(
        r"(?P<new>\d+(?:\.\d+)?)\s*(?::|/|for|op)\s*"
        r"(?P<old>\d+(?:\.\d+)?)",
        normalized,
    )
    try:
        if ratio_match is not None:
            ratio = Decimal(ratio_match.group("new")) / Decimal(
                ratio_match.group("old")
            )
        else:
            ratio = Decimal(normalized)
    except (ArithmeticError, InvalidOperation, ValueError):
        return None
    return ratio if ratio.is_finite() and ratio > 0 else None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        decimal_value = Decimal(str(value))
        return decimal_value if decimal_value.is_finite() else None
    except Exception:
        return None


def _first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value)
    return None
