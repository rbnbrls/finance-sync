"""Release 15 sync recovery drill contracts."""

# pyright: basic

import json
from pathlib import Path
from typing import Any

from scripts.sync_recovery_drill import SCENARIOS, build_report

ROOT = Path(__file__).parents[1]


def test_recovery_drill_covers_atomicity_retry_and_restart() -> None:
    report = build_report()
    assert report["database"] == "postgresql"
    assert report["queue"] == "redis"
    assert report["synthetic_data_only"] is True
    scenarios: list[dict[str, Any]] = report["scenarios"]  # type: ignore[assignment]
    assert [item["failure"] for item in scenarios] == [
        item[0] for item in SCENARIOS
    ]
    assert all(item["recovery_seconds"] >= 0 for item in scenarios)
    json.dumps(report)


def test_recovery_drill_is_a_ci_gate() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert "recovery-drill:" in workflow
    assert "image: postgres:16" in workflow
    assert "image: redis:7" in workflow
    assert "sync-recovery-drill.json" in workflow
    assert "test_sync_orchestrator_pg.py" in workflow
    assert "test_outbox_pg.py" in workflow
