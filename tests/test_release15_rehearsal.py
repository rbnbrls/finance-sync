"""Release 15 candidate rehearsal contracts."""

# pyright: basic

import json
from pathlib import Path
from typing import Any

from scripts.release_rehearsal import GATES, build_summary

ROOT = Path(__file__).parents[1]


def test_rehearsal_has_fixed_synthetic_gate_order(monkeypatch) -> None:
    monkeypatch.setenv(
        "REHEARSAL_IMAGE", "ghcr.io/rbnbrls/finance-sync:sha-abc123"
    )
    monkeypatch.setenv("REHEARSAL_COMMIT", "abc123")
    monkeypatch.setenv("REHEARSAL_SCHEMA", "alembic-head")
    summary = build_summary()
    gates: list[dict[str, Any]] = summary["gates"]  # type: ignore[assignment]
    assert [gate["name"] for gate in gates] == list(GATES)
    assert summary["synthetic_data_only"] is True
    assert summary["failures"] == []
    json.dumps(summary)


def test_release_workflow_makes_rehearsal_a_promotion_gate() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert "rehearsal:" in workflow
    assert "release-rehearsal.json" in workflow
    assert (
        "needs: [build, release-gates, migrate, security-evidence, smoke]"
        in workflow
    )
    assert "needs: operational-summary" in workflow
    assert "rollback" in workflow
