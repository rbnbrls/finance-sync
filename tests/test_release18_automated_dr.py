"""Release 18 automated DR-runbook contracts."""

# pyright: basic

import json
from pathlib import Path

import pytest

from scripts.automated_dr_runbook import EXPECTED_STEPS, build_report


def test_dr_runbook_is_idempotent_safe_and_complete() -> None:
    config = json.loads(Path("config/automated-dr-runbook.json").read_text())
    dry = build_report(config, dry_run=True)
    live = build_report(config, dry_run=False)
    assert [step["name"] for step in dry["steps"]] == list(EXPECTED_STEPS)
    assert all(step["status"] == "planned" for step in dry["steps"])
    assert all(step["status"] == "passed" for step in live["steps"])
    assert dry["production_touched"] is False
    assert live["tenant_isolation"] is True
    assert live["sync_idempotent"] is True
    assert live["outbox_validation"] == "passed"
    assert live["operational_identifiers_only"] is True


def test_invalid_runbook_order_fails() -> None:
    config = json.loads(Path("config/automated-dr-runbook.json").read_text())
    config["steps"].reverse()
    with pytest.raises(ValueError, match="incomplete or out of order"):
        build_report(config, dry_run=True)


def test_ci_runs_periodic_isolated_dr_runbook() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "automated-dr:" in workflow
    assert "automated_dr_runbook.py" in workflow
    assert "automated-dr-runbook.json" in workflow
