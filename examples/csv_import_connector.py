"""
CSV Import Connector Example
=============================

Demonstrates a connector plugin that imports financial data from CSV files.
Useful for banks / brokers that only offer CSV exports.

Usage::

    pip install finance-sync-sdk
    # Register in pyproject.toml:
    # [project.entry-points."finance_sync_sdk.plugins"]
    # csv_import = "examples.csv_import_connector:CSVImportConnector"

    config = ConnectorConfig(
        provider_type="csv_import",
        credentials={},
        options={
            "csv_path": "/path/to/transactions.csv",
            "date_format": "%Y-%m-%d",
            "delimiter": ",",
            "has_header": True,
            "column_mapping": {
                "date": "Date",
                "description": "Description",
                "amount": "Amount (EUR)",
                "type": "Transaction Type",
            },
            "currency": "EUR",
            "account_name": "My Bank CSV",
        },
    )
"""

from __future__ import annotations

import csv
import os
from datetime import UTC, datetime
from decimal import Decimal

from finance_sync_sdk import ConnectorPlugin
from finance_sync_sdk.exceptions import PermanentError
from finance_sync_sdk.models import (
    RawAccount,
    RawTransaction,
)


class CSVImportConnector(ConnectorPlugin):
    """Connector that reads financial transactions from a CSV file.

    Key concepts demonstrated:

    * File-based data source (no network I/O)
    * Configurable column mapping
    * Date format parsing
    * Account auto-creation from file metadata
    * Multiple file support (statements directory)
    """

    display_name = "CSV File Import"
    plugin_version = "0.1.0"

    # No rate limiting needed — this is file-based
    rate_limit_policy = None

    @property
    def name(self) -> str:
        return "csv_import"

    async def authenticate(self) -> None:
        """Validate that the CSV file or directory exists and is readable."""
        csv_path = self.config.options.get("csv_path", "")
        if not csv_path:
            csv_path = self.config.options.get("csv_directory", "")

        if not csv_path:
            raise PermanentError(
                "CSV import requires either 'csv_path' (single file) "
                "or 'csv_directory' (directory of CSVs) in options"
            )

        if not os.path.exists(csv_path):
            raise PermanentError(f"CSV path does not exist: {csv_path}")

        self._authenticated = True

    async def fetch_accounts(self) -> list[RawAccount]:
        """Derive accounts from CSV files.

        If a single file is specified, creates one account named after
        the file or the configured ``account_name``.
        """
        account_name = self.config.options.get(
            "account_name", "CSV Import Account"
        )

        return [
            RawAccount(
                external_account_id="csv_default",
                name=account_name,
                account_type="checking",
                currency_code=self.config.options.get("currency", "EUR"),
            )
        ]

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Parse CSV file(s) and return transactions.

        Supports:
        - Single file via ``csv_path``
        - Directory of CSV files via ``csv_directory``
        - Column mapping via ``column_mapping``
        - Date format via ``date_format``
        """
        csv_path = self.config.options.get("csv_path")
        csv_directory = self.config.options.get("csv_directory")

        if csv_path:
            files = [csv_path]
        elif csv_directory:
            files = [
                os.path.join(csv_directory, f)
                for f in sorted(os.listdir(csv_directory))
                if f.endswith(".csv")
            ]
        else:
            return []

        all_transactions: list[RawTransaction] = []
        for file_path in files:
            txns = self._parse_csv(file_path, since)
            all_transactions.extend(txns)

            if limit and len(all_transactions) >= limit:
                break

        if limit:
            all_transactions = all_transactions[:limit]

        return all_transactions

    def _parse_csv(
        self,
        file_path: str,
        since: datetime,
    ) -> list[RawTransaction]:
        """Parse a single CSV file into RawTransaction objects."""
        date_format = self.config.options.get("date_format", "%Y-%m-%d")
        delimiter = self.config.options.get("delimiter", ",")
        has_header = self.config.options.get("has_header", True)
        column_mapping = self.config.options.get("column_mapping", {})
        currency = self.config.options.get("currency", "EUR")
        account_id_from_file = f"csv_{os.path.basename(file_path)}"

        date_col = column_mapping.get("date", "Date")
        desc_col = column_mapping.get("description", "Description")
        amount_col = column_mapping.get("amount", "Amount")
        type_col = column_mapping.get("type")

        transactions: list[RawTransaction] = []

        with open(file_path, newline="", encoding="utf-8-sig") as f:
            reader = (
                csv.DictReader(f, delimiter=delimiter)
                if has_header
                else csv.DictReader(
                    f,
                    delimiter=delimiter,
                    fieldnames=[date_col, desc_col, amount_col],
                )
            )

            for row_num, row in enumerate(reader, start=1):
                try:
                    raw_date = row.get(date_col, "").strip()
                    if not raw_date:
                        continue

                    occurred_at = datetime.strptime(
                        raw_date, date_format
                    ).replace(tzinfo=UTC)
                    if occurred_at < since:
                        continue

                    raw_amount = row.get(amount_col, "0").strip()
                    # Remove currency symbols and whitespace
                    raw_amount = (
                        raw_amount.replace("\u20ac", "")
                        .replace("$", "")
                        .replace("\xa0", "")
                        .strip()
                    )
                    amount = Decimal(raw_amount.replace(",", "."))

                    description = row.get(desc_col, "").strip()
                    transaction_type = (
                        row.get(type_col, "").strip().lower()
                        if type_col
                        else ("credit" if amount > 0 else "debit")
                    )

                    txn_id = f"{account_id_from_file}_{row_num}"
                    transactions.append(
                        RawTransaction(
                            external_transaction_id=txn_id,
                            external_account_id=account_id_from_file,
                            amount=amount,
                            currency_code=currency,
                            occurred_at=occurred_at,
                            description=description,
                            transaction_type=transaction_type,
                            status="booked",
                            provider_fingerprint=str(
                                hash(f"{raw_date}{raw_amount}{description}")
                            ),
                        )
                    )
                except (ValueError, KeyError) as exc:
                    # Skip malformed rows
                    from finance_sync_sdk.exceptions import TransientError

                    raise TransientError(
                        f"Failed to parse CSV row {row_num} in "
                        f"{file_path}: {exc}"
                    ) from exc

        return transactions
