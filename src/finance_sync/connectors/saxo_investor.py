"""Importer for SaxoInvestor position and transaction exports.

SaxoInvestor supplies separate XLSX exports for positions and transactions.
This connector accepts either file or both files in one run.
"""

# The provider's Dutch export headers and messages intentionally contain
# long user-facing strings and punctuation.
# ruff: noqa: EM101,EM102,PERF401,RUF001

from __future__ import annotations

import hashlib
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawHolding,
    RawTransaction,
    SecurityReference,
)

_SUPPORTED = {".xlsx"}
_REQUIRED_HEADERS = {
    "Instrument",
    "Valuta",
    "Aantal",
    "Actuele koers",
    "Huidige waarde (EUR)",
    "ISIN",
}
_TRANSACTION_HEADERS = {
    "Transactiedatum",
    "Transactietype",
    "Boekingsbedrag",
    "Valuta",
}
_DUTCH_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mrt": 3,
    "apr": 4,
    "mei": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dec": 12,
}
_XML_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _clean(value: object) -> str:
    return str(value or "").replace("\xa0", " ").strip()


def _decimal(
    value: object, *, field: str, required: bool = True
) -> Decimal | None:
    if value is None or _clean(value) in {"", "–", "-", "—"}:
        if required:
            raise ValueError(f"{field} ontbreekt")
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = _clean(value).replace("€", "").replace("$", "").replace("£", "")
    text = text.replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field} bevat geen geldig getal: {value!r}") from exc


def _currency(value: object) -> str:
    currency = _clean(value).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"ongeldige valuta: {value!r}")
    return currency


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        parsed = datetime.fromisoformat(_clean(value))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(
        UTC
    )


def _transaction_type(action: str, transaction_type: str) -> str:
    normalized = f"{transaction_type} {action}".casefold()
    if "koop" in normalized:
        return "purchase"
    if "verkoop" in normalized:
        return "sale"
    if "dividend" in normalized:
        return "dividend"
    if "belasting" in normalized or "voorheffing" in normalized:
        return "tax"
    if "kosten" in normalized or "fee" in normalized:
        return "fee"
    if "overboeking" in normalized or "lending" in normalized:
        return "interest"
    return "other"


def _fingerprint(values: list[object]) -> str:
    payload = "\x1f".join(_clean(value).casefold() for value in values)
    return hashlib.sha256(payload.encode()).hexdigest()


def _snapshot_from_filename(path: Path) -> datetime | None:
    match = re.search(
        r"(?<!\d)(\d{1,2})[-_](jan|feb|mrt|apr|mei|jun|jul|aug|sep|okt|nov|dec)[-_](\d{4})",
        path.name.lower(),
    )
    if not match:
        return None
    day, month, year = (
        int(match.group(1)),
        _DUTCH_MONTHS[match.group(2)],
        int(match.group(3)),
    )
    return datetime(year, month, day, tzinfo=UTC)


def _snapshot_at(path: Path, configured: object) -> datetime:
    if configured:
        raw = _clean(configured)
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise PermanentError(
                "snapshot_at moet een ISO-datum zijn, bijvoorbeeld 2026-08-23"
            ) from exc
        return (
            parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        ).astimezone(UTC)
    from_name = _snapshot_from_filename(path)
    if from_name is not None:
        return from_name
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _security_type(category: str) -> str:
    normalized = category.casefold()
    if "etf" in normalized:
        return "etf"
    if "fonds" in normalized:
        return "mutual_fund"
    if "aandeel" in normalized:
        return "equity"
    return normalized.replace(" ", "_") or "other"


class SaxoInvestorConnector(Connector):
    """Read SaxoInvestor ``Posities`` and ``Transactions`` XLSX exports."""

    display_name = "SaxoInvestor Posities (Excel)"
    sdk_version = "0.1.0"
    supported_resources = frozenset({"accounts", "transactions", "holdings"})
    rate_limit_policy = None

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        self._paths: list[Path] = []
        self._position_path: Path | None = None
        self._snapshot: datetime | None = None
        self._rows: list[dict[str, Any]] = []
        self._transactions: list[RawTransaction] = []
        self._account: RawAccount | None = None

    @property
    def name(self) -> str:
        return "saxo_investor"

    @property
    def external_account_id(self) -> str:
        key = _clean(self.config.options.get("account_key")) or "default"
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", key).strip("-")[:48] or "default"
        return f"saxo-investor-{safe.casefold()}"

    async def authenticate(self) -> None:
        raw_paths = self.config.options.get("export_paths")
        if raw_paths is None:
            raw_paths = self.config.options.get("export_path")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not raw_paths:
            raise PermanentError(
                "SaxoInvestor vereist minimaal één XLSX-exportbestand."
            )
        paths = [Path(str(raw)).expanduser() for raw in raw_paths]
        for path in paths:
            if path.suffix.casefold() not in _SUPPORTED:
                raise PermanentError(
                    "SaxoInvestor ondersteunt alleen XLSX-bestanden."
                )
            if not path.is_file():
                raise PermanentError(
                    f"Kan SaxoInvestor-export niet lezen: {path.name}"
                )
        self._paths = list(dict.fromkeys(paths))
        try:
            self._load()
        except PermanentError:
            raise
        except Exception as exc:
            raise PermanentError(
                f"SaxoInvestor-export kon niet worden gelezen: {exc}"
            ) from exc
        self._authenticated = True

    async def fetch_accounts(self) -> list[RawAccount]:
        self._ensure_loaded()
        assert self._account is not None
        return [self._account]

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        self._ensure_loaded()
        if account_id is not None and account_id != self.external_account_id:
            return []
        since_utc = (
            since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        )
        result = [
            item for item in self._transactions if item.occurred_at >= since_utc
        ]
        return result[:limit] if limit is not None else result

    async def fetch_holdings(
        self, *, account_id: str | None = None
    ) -> list[RawHolding]:
        self._ensure_loaded()
        if account_id is not None and account_id != self.external_account_id:
            return []
        if not self._rows:
            return []
        assert self._snapshot is not None
        holdings: list[RawHolding] = []
        for row in self._rows:
            holdings.append(
                RawHolding(
                    external_account_id=self.external_account_id,
                    observed_at=self._snapshot,
                    quantity=row["quantity"],
                    cost_basis=row["cost_basis"],
                    cost_basis_currency=row["instrument_currency"],
                    market_value=row["market_value"],
                    currency_code="EUR",
                    price=row["price"],
                    price_currency=row["instrument_currency"],
                    security_reference=SecurityReference(
                        external_id=row["isin"] or row["symbol"],
                        isin=row["isin"] or None,
                        ticker=row["symbol"] or None,
                        name=row["instrument"],
                        venue=row["venue"] or None,
                        currency_code=row["instrument_currency"],
                        security_type=row["security_type"],
                    ),
                    provider_metadata={
                        "source": "saxoinvestor_excel_export",
                        "source_file": self._position_path.name
                        if self._position_path
                        else None,
                        "market_value_currency": "EUR",
                    },
                )
            )
        return holdings

    def _ensure_loaded(self) -> None:
        if not self._authenticated:
            raise PermanentError("SaxoInvestor-export is nog niet gevalideerd.")

    def _load(self) -> None:
        self._rows = []
        self._transactions = []
        self._position_path = None
        for path in self._paths:
            sheets = self._read_workbook(path)
            for values in sheets.values():
                if not values:
                    continue
                headers = {_clean(value) for value in values[0]}
                if _REQUIRED_HEADERS.issubset(headers):
                    self._position_path = path
                    self._rows.extend(self._parse_positions(values))
                elif _TRANSACTION_HEADERS.issubset(headers):
                    self._transactions.extend(
                        self._parse_transactions(values, path)
                    )
        if not self._rows and not self._transactions:
            raise ValueError(
                "geen herkenbare Saxo-posities of transacties gevonden "
                "(vereiste kolommen ontbreken)"
            )
        if self._position_path is not None:
            self._snapshot = _snapshot_at(
                self._position_path, self.config.options.get("snapshot_at")
            )
        total = sum((row["market_value"] for row in self._rows), Decimal(0))
        self._account = RawAccount(
            external_account_id=self.external_account_id,
            name=_clean(self.config.options.get("account_name"))
            or "SaxoInvestor",
            account_type="investment",
            account_subtype="brokerage",
            currency_code="EUR",
            current_balance=total if self._rows else None,
            available_balance=None,
            iso_currency_code="EUR",
            provider_metadata={
                "source": "saxoinvestor_excel_export",
                "snapshot_at": self._snapshot.isoformat()
                if self._snapshot
                else None,
                "holdings_count": len(self._rows),
                "transactions_count": len(self._transactions),
            },
        )

    def _parse_positions(
        self, values: list[tuple[object, ...]]
    ) -> list[dict[str, Any]]:
        headers = [_clean(value) for value in values[0]]
        index = {header: position for position, header in enumerate(headers)}
        rows: list[dict[str, Any]] = []
        for line, values_row in enumerate(values[1:], start=2):
            row = list(values_row) + [None] * max(
                0, len(headers) - len(values_row)
            )
            instrument = _clean(row[index["Instrument"]])
            isin = _clean(row[index["ISIN"]]).upper()
            if (
                not instrument
                or not isin
                or re.fullmatch(r"[A-Z]{3} \(\d+\)", instrument)
            ):
                continue
            instrument_currency = _currency(row[index["Valuta"]])
            quantity = _decimal(
                row[index["Aantal"]], field=f"Aantal op regel {line}"
            )
            price = _decimal(
                row[index["Actuele koers"]],
                field=f"Actuele koers op regel {line}",
            )
            market_value = _decimal(
                row[index["Huidige waarde (EUR)"]],
                field=f"Huidige waarde op regel {line}",
            )
            cost_basis = _decimal(
                row[index.get("Kostprijs", index["Actuele koers"])],
                field=f"Kostprijs op regel {line}",
            )
            assert (
                quantity is not None
                and price is not None
                and market_value is not None
                and cost_basis is not None
            )
            symbol = _clean(row[index.get("Symbool", index["Instrument"])])
            venue = symbol.rsplit(":", 1)[1] if ":" in symbol else ""
            rows.append(
                {
                    "instrument": instrument,
                    "isin": isin,
                    "symbol": symbol,
                    "venue": venue,
                    "instrument_currency": instrument_currency,
                    "quantity": quantity,
                    "price": price,
                    "market_value": market_value,
                    "cost_basis": cost_basis,
                    "security_type": _security_type(
                        _clean(
                            row[
                                index.get(
                                    "Soort belegging", index["Instrument"]
                                )
                            ]
                        )
                    ),
                }
            )
        return rows

    def _parse_transactions(
        self, values: list[tuple[object, ...]], path: Path
    ) -> list[RawTransaction]:
        headers = [_clean(value) for value in values[0]]
        index = {header: position for position, header in enumerate(headers)}
        result: list[RawTransaction] = []
        for line, values_row in enumerate(values[1:], start=2):
            row = list(values_row) + [None] * max(
                0, len(headers) - len(values_row)
            )
            occurred_at = _as_datetime(row[index["Transactiedatum"]])
            currency = _currency(row[index["Valuta"]])
            amount = _decimal(
                row[index["Boekingsbedrag"]],
                field=f"Boekingsbedrag op regel {line}",
            )
            assert amount is not None
            action = _clean(row[index["Acties"]])
            native_type = _clean(row[index["Transactietype"]])
            isin = _clean(row[index["Instrument ISIN"]]).upper()
            symbol = _clean(row[index["Instrumentsymbool"]])
            instrument = _clean(row[index["Instrument"]])
            reference = (
                SecurityReference(
                    external_id=isin or symbol or None,
                    isin=isin or None,
                    ticker=symbol or None,
                    name=instrument or None,
                    venue=symbol.rsplit(":", 1)[1] if ":" in symbol else None,
                    currency_code=_clean(row[index["Instrumentvaluta"]]).upper()
                    or None,
                    security_type=_security_type(_clean(row[index["Type"]])),
                )
                if (isin or symbol or instrument)
                else None
            )
            fallback_id = index.get("Bk Record Id")
            source_id = (
                row[index["Transactie-ID"]]
                if "Transactie-ID" in index
                else (row[fallback_id] if fallback_id is not None else None)
            )
            booking_id = (
                row[index["Booking Id"]]
                if "Booking Id" in index
                else (row[fallback_id] if fallback_id is not None else None)
            )
            external_id = (
                "saxo-"
                + _fingerprint(
                    [
                        source_id,
                        booking_id,
                        occurred_at,
                        native_type,
                        action,
                        amount,
                        currency,
                        isin,
                        line,
                    ]
                )[:32]
            )
            fee = _decimal(
                row[index.get("Totale kosten", index["Boekingsbedrag"])],
                field="Totale kosten",
                required=False,
            ) or Decimal(0)
            result.append(
                RawTransaction(
                    external_transaction_id=external_id,
                    external_account_id=self.external_account_id,
                    amount=amount,
                    currency_code=currency,
                    occurred_at=occurred_at,
                    booked_at=_as_datetime(row[index["Valutadatum"]])
                    if row[index["Valutadatum"]]
                    else None,
                    description=action or _clean(row[index["Opmerking"]]),
                    transaction_type=_transaction_type(action, native_type),
                    status="booked",
                    provider_fingerprint=external_id,
                    security_reference=reference,
                    fee_amount=abs(fee) if fee else None,
                    fee_currency_code=currency if fee else None,
                    amount_in_base=amount,
                    base_currency_code=currency,
                    fx_rate=(
                        _decimal(
                            row[index["Omrekeningskoers"]],
                            field="Omrekeningskoers",
                            required=False,
                        )
                        if "Omrekeningskoers" in index
                        else None
                    ),
                    provider_metadata={
                        "source": "saxoinvestor_excel_export",
                        "source_file": path.name,
                        "booking_id": _clean(booking_id),
                        "transaction_id": _clean(source_id),
                        "account_id": _clean(row[index["Rekening-ID"]]),
                    },
                )
            )
        return result

    @classmethod
    def _read_workbook(cls, path: Path) -> dict[str, list[tuple[object, ...]]]:
        """Read all worksheets, with an XML fallback for malformed styles."""
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                return {
                    sheet.title: [tuple(row) for row in sheet.values]
                    for sheet in workbook.worksheets
                }
            finally:
                workbook.close()
        except Exception as openpyxl_error:
            try:
                return {"Sheet1": cls._read_xml_values(path)}
            except Exception as xml_error:
                raise ValueError(
                    f"XLSX kon niet worden gelezen ({openpyxl_error})"
                ) from xml_error

    @staticmethod
    def _read_xml_values(path: Path) -> list[tuple[object, ...]]:
        """Read the first worksheet, tolerating Saxo's non-standard styles.

        The normal openpyxl path is preferred.  Some Saxo exports contain a
        style XML node that openpyxl rejects even though the worksheet and
        shared strings are valid, so a narrow XML reader is retained as a
        compatibility fallback.
        """
        with zipfile.ZipFile(path) as archive:
            shared_root = ElementTree.fromstring(
                archive.read("xl/sharedStrings.xml")
            )
            shared = [
                "".join(
                    text.text or "" for text in item.findall(".//m:t", _XML_NS)
                )
                for item in shared_root.findall("m:si", _XML_NS)
            ]
            sheet_root = ElementTree.fromstring(
                archive.read("xl/worksheets/sheet1.xml")
            )

        rows: list[tuple[object, ...]] = []
        for row_node in sheet_root.findall(".//m:sheetData/m:row", _XML_NS):
            cells: dict[int, object] = {}
            for cell in row_node.findall("m:c", _XML_NS):
                reference = cell.attrib.get("r", "")
                letters = re.match(r"[A-Z]+", reference)
                if not letters:
                    continue
                column = 0
                for letter in letters.group():
                    column = column * 26 + ord(letter) - ord("A") + 1
                value_node = cell.find("m:v", _XML_NS)
                if value_node is None:
                    value: object = None
                else:
                    raw = value_node.text or ""
                    if cell.attrib.get("t") == "s":
                        value = shared[int(raw)]
                    else:
                        try:
                            value = Decimal(raw)
                        except InvalidOperation:
                            value = raw
                cells[column - 1] = value
            width = max(cells, default=-1) + 1
            rows.append(tuple(cells.get(index) for index in range(width)))
        return rows
