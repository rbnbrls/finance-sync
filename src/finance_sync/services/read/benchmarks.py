"""Deterministic dataset profiles for read performance tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReadBenchmark:
    """Dataset shape used by a reproducible read benchmark."""

    name: str
    account_count: int
    holding_count: int


READ_BENCHMARKS: tuple[ReadBenchmark, ...] = (
    ReadBenchmark("holdings-100", account_count=5, holding_count=100),
    ReadBenchmark("holdings-1000", account_count=20, holding_count=1000),
)
