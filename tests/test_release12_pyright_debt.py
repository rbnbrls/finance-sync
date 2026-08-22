"""Regression contract for the Release 12 Pyright warning ratchet."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_warning_budget_is_ratcheted_to_sixty() -> None:
    config = json.loads(
        (PROJECT_ROOT / "config/pyright-warning-budget.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["max_warnings"] == 60


def test_warning_debt_has_module_and_cause_classification() -> None:
    document = (PROJECT_ROOT / "docs/PYRIGHT_WARNING_DEBT.md").read_text(
        encoding="utf-8"
    )
    assert "Private usage" in document
    assert "Missing stubs" in document
    assert "Argument type" in document
    assert "**Total** | **54** | **4** | **2** | **60**" in document
