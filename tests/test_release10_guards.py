"""Regression guards for Release 10 performance contracts."""

from sqlalchemy import create_engine, text

from finance_sync.services.read.benchmarks import READ_BENCHMARKS
from finance_sync.services.read.query_counter import QueryCounter


def test_benchmark_profiles_are_deterministic() -> None:
    assert [(item.name, item.holding_count) for item in READ_BENCHMARKS] == [
        ("holdings-100", 100),
        ("holdings-1000", 1000),
    ]
    assert all(item.account_count > 0 for item in READ_BENCHMARKS)


def test_query_counter_can_be_used_as_a_context_manager() -> None:
    engine = create_engine("sqlite://")
    with QueryCounter(engine) as counter, engine.connect() as connection:
        connection.execute(text("select 1"))
    assert counter.queries == 1
    engine.dispose()
