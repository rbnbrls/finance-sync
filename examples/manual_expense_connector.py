"""
Manual Expense Tracking Connector Example
==========================================

Demonstrates a connector plugin for manually entered expenses and cash
transactions.  Users record expenses via a simple JSON/YAML file or
in-memory dict, and the connector feeds them into the finance-sync pipeline.

This is useful for:
- Cash transactions (ATM withdrawals with notes on what the cash paid for)
- Splitwise / IOUs between friends
- One-off manual corrections
- Expenses from providers without API/CSV access

Usage::

    pip install finance-sync-sdk
    # Register in pyproject.toml:
    # [project.entry-points."finance_sync_sdk.plugins"]
    # manual_expense = "examples.manual_expense_connector:ManualExpenseConnector"

    config = ConnectorConfig(
        provider_type="manual_expense",
        credentials={},
        options={
            "data_path": "/path/to/expenses.json",
            "default_currency": "EUR",
            "account_name": "Cash Wallet",
        },
    )
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal

from finance_sync_sdk import ConnectorPlugin
from finance_sync_sdk.exceptions import PermanentError
from finance_sync_sdk.models import (
    RawAccount,
    RawTransaction,
)


class ManualExpenseConnector(ConnectorPlugin):
    """Connector for manually recorded expenses and cash transactions.

    Key concepts demonstrated:

    * JSON/YAML file-based data source
    * Categorisation with tags
    * Receipt / photo attachment references
    * Split transactions (multiple categories per payment)
    * Recurring expense pattern (subscriptions)
    """

    display_name = "Manual Expenses"
    plugin_version = "0.1.0"

    @property
    def name(self) -> str:
        return "manual_expense"

    async def authenticate(self) -> None:
        """Validate the data source file exists and is readable."""
        data_path = self._get_data_path()
        if not data_path or not os.path.exists(data_path):
            # First run — data file will be created
            self._authenticated = True
            return

        try:
            with open(data_path) as f:
                json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise PermanentError(f"Invalid expense data file: {exc}") from exc

        self._authenticated = True

    async def fetch_accounts(self) -> list[RawAccount]:
        """Create a manual wallet account."""
        account_name = self.config.options.get("account_name", "Cash Wallet")
        return [
            RawAccount(
                external_account_id="manual_wallet",
                name=account_name,
                account_type="checking",
                currency_code=self.config.options.get(
                    "default_currency", "EUR"
                ),
            )
        ]

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        """Read expenses from the data source and return as transactions.

        Expected data format (JSON)::

            {
                "expenses": [
                    {
                        "id": "exp_001",
                        "date": "2025-01-15",
                        "amount": -45.00,
                        "currency": "EUR",
                        "description": "Lunch with team",
                        "category": "Food & Dining",
                        "tags": ["work", "team"],
                        "recurring": false,
                        "receipt_path": "/photos/lunch_receipt.jpg"
                    }
                ]
            }
        """
        data_path = self._get_data_path()
        if not data_path or not os.path.exists(data_path):
            return []

        with open(data_path) as f:
            data = json.load(f)

        expenses = data.get("expenses", [])
        _max = limit or len(expenses)
        transactions: list[RawTransaction] = []

        for exp in expenses[:_max]:
            occurred_at = datetime.fromisoformat(exp["date"]).replace(
                tzinfo=UTC
            )
            if occurred_at < since:
                continue

            amount = Decimal(str(exp["amount"]))

            # Build a rich description from available fields
            desc_parts = [exp.get("description", "")]
            if exp.get("category"):
                desc_parts.append(f"[{exp['category']}]")
            if exp.get("tags"):
                desc_parts.append(f"#{' #'.join(exp['tags'])}")
            description = " ".join(desc_parts)

            transactions.append(
                RawTransaction(
                    external_transaction_id=f"manual_{exp['id']}",
                    external_account_id="manual_wallet",
                    amount=amount,
                    currency_code=exp.get("currency", "EUR"),
                    occurred_at=occurred_at,
                    description=description,
                    transaction_type="expense" if amount < 0 else "income",
                    status="booked",
                    provider_metadata={
                        "category": exp.get("category"),
                        "tags": exp.get("tags", []),
                        "recurring": exp.get("recurring", False),
                        "receipt_path": exp.get("receipt_path"),
                        "source": "manual_entry",
                    },
                )
            )

        return transactions

    def _get_data_path(self) -> str | None:
        """Resolve the data file path from config."""
        data_path = self.config.options.get("data_path")
        if data_path:
            data_path = os.path.expanduser(data_path)
        return data_path

    # ── Sample: create a template data file ─────────────────────────────

    @staticmethod
    def create_template(path: str) -> None:
        """Create a template expenses JSON file.

        Usage::

            ManualExpenseConnector.create_template("./my_expenses.json")
        """
        template = {
            "$schema": "Manual Expense Data v1",
            "description": "Add your expenses as JSON objects in the 'expenses' array",
            "expenses": [
                {
                    "id": "exp_001",
                    "date": "2025-01-15",
                    "amount": -45.00,
                    "currency": "EUR",
                    "description": "Lunch with team",
                    "category": "Food & Dining",
                    "tags": ["work", "lunch"],
                    "recurring": False,
                    "receipt_path": None,
                },
                {
                    "id": "exp_002",
                    "date": "2025-01-16",
                    "amount": -120.00,
                    "currency": "EUR",
                    "description": "Monthly electricity bill",
                    "category": "Utilities",
                    "tags": ["bills", "recurring"],
                    "recurring": True,
                    "receipt_path": None,
                },
                {
                    "id": "exp_003",
                    "date": "2025-01-20",
                    "amount": 500.00,
                    "currency": "EUR",
                    "description": "Freelance payment received",
                    "category": "Income",
                    "tags": ["freelance"],
                    "recurring": False,
                    "receipt_path": None,
                },
            ],
        }
        with open(path, "w") as f:
            json.dump(template, f, indent=2)
        print(f"Template created at {path}")
