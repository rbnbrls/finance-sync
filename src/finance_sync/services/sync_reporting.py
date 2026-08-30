"""Small, deterministic sync outcome counters shared by workers and API."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class SyncReport:
    counts: Counter[str] = field(default_factory=lambda: Counter[str]())

    def record(self, outcome: str, amount: int = 1) -> None:
        self.counts[outcome] += amount

    def as_dict(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))
