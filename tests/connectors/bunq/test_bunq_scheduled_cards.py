"""Tests for scheduled payments and card transaction ingestion.

Uses the same mock HTTP transport setup as the bunq connector tests.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from finance_sync.connectors.bunq import (
    _map_auth_status,
    _map_card_payment_status,
    _map_schedule_status,
)
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import (
    CanonicalCardTransactionData,
    CanonicalScheduledPaymentData,
    RawCardTransaction,
    RawScheduledPayment,
)

if TYPE_CHECKING:
    from finance_sync.connectors.bunq import BunqConnector


class TestScheduledPayments:
    """Contract tests for scheduled payment ingestion."""

    pytestmark = pytest.mark.asyncio

    # ── Fetch ──────────────────────────────────────────────────────────

    async def test_fetch_scheduled_payments_returns_list(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_scheduled_payments returns a list of RawScheduledPayment."""
        await bunq_connector.authenticate()
        schedules = await bunq_connector.fetch_scheduled_payments()

        assert isinstance(schedules, list)
        assert len(schedules) == 2  # 2 schedules on account 1000001

        # First schedule should be the monthly rent
        monthly = schedules[0]
        assert isinstance(monthly, RawScheduledPayment)
        assert monthly.external_schedule_id == "3000001"
        assert monthly.amount == Decimal("-150.00")
        assert monthly.frequency == "MONTHLY"
        assert monthly.interval == 1
        assert monthly.execution_count == 6
        assert monthly.counterparty_name == "Landlord B.V."
        assert monthly.description == "Monthly rent"

        # Second should be the weekly subscription
        weekly = schedules[1]
        assert isinstance(weekly, RawScheduledPayment)
        assert weekly.external_schedule_id == "3000002"
        assert weekly.amount == Decimal("-25.00")
        assert weekly.frequency == "WEEKLY"
        assert weekly.execution_count == 0
        assert weekly.counterparty_name == "Streaming Co."

    async def test_fetch_scheduled_payments_with_account_filter(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_scheduled_payments should accept an account_id filter."""
        await bunq_connector.authenticate()
        schedules = await bunq_connector.fetch_scheduled_payments(
            account_id="1000001"
        )

        assert isinstance(schedules, list)
        assert len(schedules) == 2

    async def test_fetch_scheduled_payments_not_authenticated(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Calling fetch before authenticate should raise."""
        with pytest.raises(PermanentError, match="not authenticated"):
            await bunq_connector.fetch_scheduled_payments()

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_scheduled_payments(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Transform: RawScheduledPayment -> CanonicalScheduledPaymentData."""
        await bunq_connector.authenticate()
        raw = await bunq_connector.fetch_scheduled_payments()
        assert len(raw) >= 1

        # Apply default transform (identity mapping)
        canonical = [
            CanonicalScheduledPaymentData(
                provider_key=bunq_connector.name,
                external_schedule_id=r.external_schedule_id,
                external_account_id=r.external_account_id,
                amount=r.amount,
                currency_code=r.currency_code,
                frequency=r.frequency.lower(),
                interval=r.interval,
                next_execution_date=r.next_execution_date,
                end_date=r.end_date,
                max_executions=r.max_executions,
                execution_count=r.execution_count or 0,
                counterparty_name=r.counterparty_name,
                counterparty_iban=r.counterparty_iban,
                description=r.description,
                status=r.status or "active",
            )
            for r in raw
        ]

        assert len(canonical) == 2
        assert all(
            isinstance(c, CanonicalScheduledPaymentData) for c in canonical
        )

        monthly = canonical[0]
        assert monthly.amount == Decimal("-150.00")
        assert monthly.frequency == "monthly"
        assert monthly.execution_count == 6
        assert monthly.counterparty_name == "Landlord B.V."

    # ── Mapping helpers ────────────────────────────────────────────────

    def test_map_schedule_status(self) -> None:
        """Schedule status mapping should work correctly."""
        assert _map_schedule_status("ACTIVE") == "active"
        assert _map_schedule_status("INACTIVE") == "paused"
        assert _map_schedule_status("CANCELLED") == "cancelled"
        assert _map_schedule_status("COMPLETED") == "completed"
        assert _map_schedule_status("FAILED") == "failed"
        assert _map_schedule_status("UNKNOWN") == "active"
        assert _map_schedule_status("") == "active"


class TestCardTransactions:
    """Contract tests for card transaction ingestion."""

    pytestmark = pytest.mark.asyncio

    # ── Fetch ──────────────────────────────────────────────────────────

    async def test_fetch_card_transactions_returns_list(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_card_transactions returns a list of RawCardTransaction."""
        await bunq_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await bunq_connector.fetch_card_transactions(since=since)

        assert isinstance(txns, list)
        assert len(txns) == 3  # 3 card payments on card 7000001

        # First should be the authorization (most recent)
        auth = txns[0]
        assert isinstance(auth, RawCardTransaction)
        assert auth.external_card_transaction_id == "8000001"
        assert auth.amount == Decimal("-42.50")
        assert auth.merchant_name == "Supermarket B.V."
        assert auth.merchant_city == "Amsterdam"
        assert auth.mcc == "5411"
        assert auth.authorization_type == "authorization"
        assert auth.card_id == "7000001"
        assert auth.card_type == "DEBIT_CARD"

        # Second should be the settlement
        settlement = txns[1]
        assert settlement.amount == Decimal("-89.99")
        assert settlement.authorization_type == "settlement"
        assert settlement.merchant_name == "Online Store"

        # Third should be the refund
        refund = txns[2]
        assert refund.amount == Decimal("15.99")
        assert refund.authorization_type == "refund"

    async def test_fetch_card_transactions_with_limit(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_card_transactions should accept a limit parameter."""
        await bunq_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await bunq_connector.fetch_card_transactions(
            since=since, limit=2
        )
        assert isinstance(txns, list)
        assert len(txns) <= 2

    async def test_fetch_card_transactions_since_filter(
        self, bunq_connector: BunqConnector
    ) -> None:
        """fetch_card_transactions should respect the since parameter."""
        await bunq_connector.authenticate()
        since = datetime(2025, 6, 18, tzinfo=UTC)
        txns = await bunq_connector.fetch_card_transactions(since=since)
        assert isinstance(txns, list)
        # Should include authorization (June 20) and settlement
        # (June 18 at 10:00 >= June 18 at 00:00 UTC)
        assert len(txns) >= 2

    async def test_fetch_card_transactions_not_authenticated(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Calling fetch_card_transactions before auth should raise."""
        since = datetime(2025, 1, 1, tzinfo=UTC)
        with pytest.raises(PermanentError, match="not authenticated"):
            await bunq_connector.fetch_card_transactions(since=since)

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_card_transactions(
        self, bunq_connector: BunqConnector
    ) -> None:
        """Transform: RawCardTransaction -> CanonicalCardTransactionData."""
        await bunq_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        raw = await bunq_connector.fetch_card_transactions(since=since)
        assert len(raw) >= 1

        # Apply default transform (identity mapping)
        canonical = [
            CanonicalCardTransactionData(
                provider_key=bunq_connector.name,
                external_card_transaction_id=r.external_card_transaction_id,
                external_account_id=r.external_account_id,
                amount=r.amount,
                currency_code=r.currency_code,
                merchant_name=r.merchant_name,
                merchant_city=r.merchant_city,
                merchant_country=r.merchant_country,
                mcc=r.mcc,
                card_id=r.card_id,
                card_type=r.card_type,
                card_last_four=r.card_last_four,
                occurred_at=r.occurred_at,
                booked_at=r.booked_at,
                authorization_type=r.authorization_type or "authorization",
                description=r.description,
                status=r.status or "pending",
            )
            for r in raw
        ]

        assert len(canonical) == 3
        assert all(
            isinstance(c, CanonicalCardTransactionData) for c in canonical
        )

        auth = canonical[0]
        assert auth.amount == Decimal("-42.50")
        assert auth.merchant_name == "Supermarket B.V."
        assert auth.mcc == "5411"
        assert auth.authorization_type == "authorization"
        assert auth.status == "pending"

        settlement = canonical[1]
        assert settlement.amount == Decimal("-89.99")
        assert settlement.authorization_type == "settlement"
        assert settlement.status == "booked"

        refund = canonical[2]
        assert refund.amount == Decimal("15.99")
        assert refund.authorization_type == "refund"
        assert refund.status == "booked"

    # ── Mapping helpers ────────────────────────────────────────────────

    def test_map_auth_status(self) -> None:
        """Card auth status mapping should work correctly."""
        assert _map_auth_status("AUTHORISATION") == "authorization"
        assert _map_auth_status("AUTHORIZATION") == "authorization"
        assert _map_auth_status("SETTLEMENT") == "settlement"
        assert _map_auth_status("REFUND") == "refund"
        assert _map_auth_status("CHARGEBACK") == "chargeback"
        assert _map_auth_status("UNKNOWN") == "authorization"

    def test_map_card_payment_status(self) -> None:
        """Card payment status mapping should work correctly."""
        assert _map_card_payment_status("AUTHORISATION") == "pending"
        assert _map_card_payment_status("SETTLEMENT") == "booked"
        assert _map_card_payment_status("REFUND") == "booked"
        assert _map_card_payment_status("COMPLETED") == "booked"
        assert _map_card_payment_status("CANCELLED") == "cancelled"
        assert _map_card_payment_status("REVERSED") == "reversed"
        assert _map_card_payment_status("UNKNOWN") == "pending"
