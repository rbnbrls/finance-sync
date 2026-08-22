"""Characterization tests for the orchestration/persistence boundary."""

import inspect
from pathlib import Path

from finance_sync.sync.orchestrator import SyncOrchestrator
from finance_sync.sync.persistence import (
    AccountPersistence,
    CardsPersistence,
    HoldingPersistence,
    SecurityPersistence,
    TransactionPersistence,
)

ORCHESTRATOR = (
    Path(__file__).parents[1] / "src/finance_sync/sync/orchestrator.py"
)


def test_orchestrator_is_coordinating_only() -> None:
    source = ORCHESTRATOR.read_text(encoding="utf-8")

    assert len(source.splitlines()) <= 900
    assert "_upsert_" not in source
    assert "_resolve_security_reference" not in source
    assert "class SyncOrchestrator(CardsSyncMixin)" in source

    methods = set(vars(SyncOrchestrator))
    assert not any(name.startswith("_upsert_") for name in methods)
    assert not hasattr(SyncOrchestrator, "_resolve_security_reference")


def test_entity_persistence_has_explicit_component_owners() -> None:
    assert inspect.iscoroutinefunction(AccountPersistence.persist_account)
    assert inspect.iscoroutinefunction(
        TransactionPersistence.persist_transaction
    )
    assert inspect.iscoroutinefunction(HoldingPersistence.persist_holding)
    assert inspect.iscoroutinefunction(
        SecurityPersistence.resolve_security_reference
    )
    assert inspect.iscoroutinefunction(
        CardsPersistence.persist_scheduled_payment
    )
    assert inspect.iscoroutinefunction(
        CardsPersistence.persist_card_transaction
    )


def test_account_persistence_is_not_reimplemented_in_orchestrator() -> None:
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    assert "_upsert_account" not in source
    assert "Account(" not in source
    assert "finance_sync.models.account" not in source
    assert inspect.iscoroutinefunction(AccountPersistence.persist_account)


def test_security_resolution_is_owned_by_persistence_dependency() -> None:
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    assert "_resolve_security_reference" not in source
    assert "resolve_security_reference" not in vars(SyncOrchestrator)
    assert inspect.iscoroutinefunction(
        SecurityPersistence.resolve_security_reference
    )


def test_transaction_and_holding_persistence_are_not_in_orchestrator() -> None:
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    assert "_upsert_transaction" not in source
    assert "_upsert_holding" not in source
    assert "Transaction(" not in source
    assert "Holding(" not in source
    assert inspect.iscoroutinefunction(
        TransactionPersistence.persist_transaction
    )
    assert inspect.iscoroutinefunction(HoldingPersistence.persist_holding)


def test_scheduled_and_card_writes_are_component_owned() -> None:
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    assert "ScheduledPayment(" not in source
    assert "CardTransaction(" not in source
    assert inspect.iscoroutinefunction(
        CardsPersistence.persist_scheduled_payment
    )
    assert inspect.iscoroutinefunction(
        CardsPersistence.persist_card_transaction
    )
