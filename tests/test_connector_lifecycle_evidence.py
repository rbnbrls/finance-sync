from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.connector_lifecycle_evidence import build_evidence

ROOT = Path(__file__).parents[1]


def _fixtures() -> tuple[dict, dict]:
    lifecycle = json.loads((ROOT / "config/connector-lifecycle.json").read_text())
    matrix = json.loads((ROOT / "config/provider-contract-matrix.json").read_text())
    return lifecycle, matrix


def test_evidence_contains_required_safe_release_fields() -> None:
    lifecycle, matrix = _fixtures()
    evidence = build_evidence(lifecycle, matrix, test_result="passed", canary_result="passed")
    assert evidence["release_gate"] == "passed"
    assert evidence["synthetic_data_only"] is True
    assert all(
        set(item) == {
            "provider", "connector_version", "certification_commit", "fixture_version",
            "test_result", "canary_result", "rollback_version",
        }
        for item in evidence["connectors"]
    )


def test_evidence_rejects_uncertified_release() -> None:
    lifecycle, matrix = _fixtures()
    lifecycle["connectors"][0]["certification_status"] = "pending"
    with pytest.raises(ValueError, match="not certified"):
        build_evidence(lifecycle, matrix, test_result="passed", canary_result="passed")
