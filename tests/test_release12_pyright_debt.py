"""Regression contract for the Release 12 Pyright warning ratchet."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_warning_budget_matches_current_source_baseline() -> None:
    config = json.loads(
        (PROJECT_ROOT / "config/pyright-warning-budget.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["max_warnings"] == 138


def test_warning_debt_has_module_and_cause_classification() -> None:
    document = (PROJECT_ROOT / "docs/PYRIGHT_WARNING_DEBT.md").read_text(
        encoding="utf-8"
    )
    assert "Unknown type" in document
    assert "Optional/member" in document
    assert "Argument type" in document
    assert "**Total** | **116** | **5** | **7** | **10** | **138**" in document
