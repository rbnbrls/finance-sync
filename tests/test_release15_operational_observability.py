"""Release 15 operational gate-summary contracts."""

import json
import os
import tempfile
from pathlib import Path

from scripts.operational_gate_summary import REQUIRED_GATES, build_summary


def test_summary_requires_fresh_artifacts_and_exposes_safe_health() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifacts = {gate: root / f"{gate}.json" for gate in REQUIRED_GATES}
        for path in artifacts.values():
            path.write_text("{}", encoding="utf-8")
        reference_time = artifacts["unit"].stat().st_mtime
        summary = build_summary(
            artifacts,
            now=reference_time,
            max_age_hours=1,
            sync_status="healthy",
            outbox_lag="0",
        )
        assert summary["failures"] == []
        assert summary["sync_health"] == {
            "status": "healthy",
            "outbox_lag": "0",
        }
        assert summary["contains_financial_data"] is False
        assert summary["contains_secrets"] is False
        os.utime(artifacts["benchmark"], (0, 0))
        stale = build_summary(
            artifacts,
            now=reference_time + 3601,
            max_age_hours=1,
            sync_status="unknown",
            outbox_lag="unknown",
        )
        assert "benchmark: artifact too old" in stale["failures"]
        json.dumps(stale)


def test_release_workflow_publishes_operator_summary() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "operational-summary:" in workflow
    assert "operational_gate_summary.py" in workflow
    for gate in REQUIRED_GATES:
        assert f"{gate}=" in workflow
