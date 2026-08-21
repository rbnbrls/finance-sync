"""Regression tests for the Release 8 persistence boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finance_sync.sync.persistence import PersistenceContext, SyncPersistence


@pytest.mark.asyncio
async def test_sync_persistence_forwards_writes_without_transaction_control() -> (
    None
):
    writer = SimpleNamespace(
        persist_account=AsyncMock(return_value="account"),
        persist_transaction=AsyncMock(return_value="transaction"),
        persist_holding=AsyncMock(return_value="holding"),
        resolve_security_reference=AsyncMock(return_value=(None, "ISIN")),
    )
    persistence = SyncPersistence(writer)
    uow = object()
    account = object()
    transaction = object()
    holding = object()
    reference = object()

    assert await persistence.persist_account(uow, account) == "account"
    assert (
        await persistence.persist_transaction(
            uow,
            transaction,
            "account-id",
            security_id="security-id",
            connection_id="connection-id",
        )
        == "transaction"
    )
    assert (
        await persistence.persist_holding(
            uow, holding, "account-id", "security-id"
        )
        == "holding"
    )
    assert await persistence.resolve_security_reference(
        uow, "provider", reference
    ) == (None, "ISIN")

    writer.persist_account.assert_awaited_once_with(
        uow, account, connection_id=None
    )
    writer.persist_transaction.assert_awaited_once_with(
        uow,
        transaction,
        "account-id",
        security_id="security-id",
        connection_id="connection-id",
    )
    writer.persist_holding.assert_awaited_once_with(
        uow, holding, "account-id", "security-id"
    )
    writer.resolve_security_reference.assert_awaited_once_with(
        uow, "provider", reference
    )
    assert not hasattr(persistence, "commit")
    assert not hasattr(persistence, "rollback")


def test_persistence_context_is_immutable_and_explicit() -> None:
    context = PersistenceContext(
        tenant_id="tenant",
        provider_type="provider",
        connection_id="connection",
    )
    assert context.provider_type == "provider"
    with pytest.raises(AttributeError):
        context.tenant_id = "other"  # type: ignore[misc]
