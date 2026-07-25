"""Tests for the sync outbox helpers.

Tests the add_outbox_message function and the convenience wrappers
for entity creation, updates, and reconciliation completion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.sync.outbox import (
    add_outbox_message,
    outbox_entity_created,
    outbox_entity_updated,
    outbox_reconciliation_completed,
)


@pytest.fixture
def uow() -> MagicMock:
    """Mock UnitOfWork with an async session."""
    uow = MagicMock()
    uow.session = AsyncMock()
    return uow


class TestAddOutboxMessage:
    """Tests for the base add_outbox_message function."""

    async def test_creates_message(self, uow: MagicMock) -> None:
        """A new OutboxMessage is added to the session."""
        msg = await add_outbox_message(
            uow,
            aggregate_id="agg-1",
            aggregate_type="reconciliation",
            event_type="reconciliation.completed",
            payload={"finding_count": 5},
            idempotency_key="reconciliation:agg-1:completed",
        )

        assert msg.aggregate_id == "agg-1"
        assert msg.aggregate_type == "reconciliation"
        assert msg.event_type == "reconciliation.completed"
        assert msg.payload == {"finding_count": 5}
        assert msg.idempotency_key == "reconciliation:agg-1:completed"
        uow.session.add.assert_called_once_with(msg)

    async def test_default_payload(self, uow: MagicMock) -> None:
        """When payload is None, it defaults to empty dict."""
        msg = await add_outbox_message(
            uow,
            aggregate_id="agg-2",
            aggregate_type="test",
            event_type="test.event",
        )
        assert msg.payload == {}

    async def test_default_idempotency_key(self, uow: MagicMock) -> None:
        """When idempotency_key is None, it stays None."""
        msg = await add_outbox_message(
            uow,
            aggregate_id="agg-3",
            aggregate_type="test",
            event_type="test.event",
        )
        assert msg.idempotency_key is None


class TestOutboxEntityCreated:
    """Tests for the outbox_entity_created convenience wrapper."""

    async def test_creates_entity_created_message(self, uow: MagicMock) -> None:
        """Creates a {entity_type}.created outbox message."""
        msg = await outbox_entity_created(
            uow,
            entity_type="account",
            entity_id="acct-uuid-1",
            entity_data={"name": "Test Account", "provider_key": "bunq"},
            provider_key="bunq",
        )

        assert msg.aggregate_id == "acct-uuid-1"
        assert msg.aggregate_type == "account"
        assert msg.event_type == "account.created"
        assert msg.idempotency_key == "account:acct-uuid-1:created"
        assert msg.payload["entity_id"] == "acct-uuid-1"
        assert msg.payload["entity_type"] == "account"
        assert msg.payload["data"]["name"] == "Test Account"
        assert msg.payload["provider_key"] == "bunq"

    async def test_no_entity_data(self, uow: MagicMock) -> None:
        """entity_data defaults to empty dict."""
        msg = await outbox_entity_created(
            uow,
            entity_type="transaction",
            entity_id="txn-uuid-1",
            provider_key="bunq",
        )
        assert msg.payload["data"] == {}


class TestOutboxEntityUpdated:
    """Tests for the outbox_entity_updated convenience wrapper."""

    async def test_creates_entity_updated_message(self, uow: MagicMock) -> None:
        """Creates a {entity_type}.updated outbox message."""
        msg = await outbox_entity_updated(
            uow,
            entity_type="account",
            entity_id="acct-uuid-1",
            changed_fields={"name": "New Name", "current_balance": "1500.00"},
            provider_key="bunq",
        )

        assert msg.aggregate_id == "acct-uuid-1"
        assert msg.aggregate_type == "account"
        assert msg.event_type == "account.updated"
        assert msg.idempotency_key == "account:acct-uuid-1:updated"
        assert msg.payload["changed_fields"]["name"] == "New Name"
        assert msg.payload["provider_key"] == "bunq"

    async def test_no_changed_fields(self, uow: MagicMock) -> None:
        """changed_fields defaults to empty dict."""
        msg = await outbox_entity_updated(
            uow,
            entity_type="transaction",
            entity_id="txn-uuid-1",
            provider_key="trading212",
        )
        assert msg.payload["changed_fields"] == {}


class TestOutboxReconciliationCompleted:
    """Tests for the outbox_reconciliation_completed convenience wrapper."""

    async def test_creates_completed_message(self, uow: MagicMock) -> None:
        """Creates a reconciliation.completed outbox message."""
        msg = await outbox_reconciliation_completed(
            uow,
            run_id="run-uuid-1",
            tenant_id="tenant-1",
            finding_count=5,
            summary={
                "by_kind": {"duplicate_transaction": 3, "missing_transaction": 2},
                "by_severity": {"warning": 3, "info": 2},
            },
        )

        assert msg.aggregate_id == "run-uuid-1"
        assert msg.aggregate_type == "reconciliation"
        assert msg.event_type == "reconciliation.completed"
        assert msg.idempotency_key == "reconciliation:run-uuid-1:completed"
        assert msg.payload["run_id"] == "run-uuid-1"
        assert msg.payload["tenant_id"] == "tenant-1"
        assert msg.payload["finding_count"] == 5
        assert msg.payload["summary"]["by_kind"]["duplicate_transaction"] == 3

    async def test_default_summary(self, uow: MagicMock) -> None:
        """When summary is None, it defaults to empty dict."""
        msg = await outbox_reconciliation_completed(
            uow,
            run_id="run-uuid-2",
            tenant_id="tenant-1",
            finding_count=0,
        )
        assert msg.payload["summary"] == {}

    async def test_zero_findings(self, uow: MagicMock) -> None:
        """Zero finding_count is recorded correctly."""
        msg = await outbox_reconciliation_completed(
            uow,
            run_id="run-uuid-3",
            tenant_id="tenant-1",
            finding_count=0,
        )
        assert msg.payload["finding_count"] == 0
