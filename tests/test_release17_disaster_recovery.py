"""Release 17 disaster-recovery game-day contracts."""

# pyright: basic

import json
from pathlib import Path

from scripts.dr_game_day import SCENARIOS, build_report


def test_game_day_covers_loss_modes_and_safe_recovery() -> None:
    config = json.loads(Path("config/dr-game-day.json").read_text())
    report = build_report(config)
    assert {item["name"] for item in report["scenarios"]} == set(SCENARIOS)
    assert all(item["status"] == "recovered" for item in report["scenarios"])
    assert all(item["tenant_isolation"] for item in report["scenarios"])
    assert all(item["idempotent_replay"] for item in report["scenarios"])
    assert report["credentials_detected"] is False
    assert report["financial_values_in_report"] is False
    assert all(action["owner"] and action["deadline"] for action in report["actions"])


def test_ci_runs_and_uploads_the_game_day() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "dr-game-day:" in workflow
    assert "dr_game_day.py" in workflow
    assert "dr-game-day.json" in workflow
