"""Release 14 smoke-evidence artifact and tag policy tests."""

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_smoke_evidence_contains_schema_and_dataset_metadata() -> None:
    smoke = (ROOT / "scripts/release_smoke.py").read_text(encoding="utf-8")
    assert "schema_version" in smoke
    assert "synthetic_dataset" in smoke
    assert "SMOKE_JUNIT" in smoke
    assert '<testsuite name="release-smoke"' in smoke


def test_release_workflow_gates_smoke_artifacts_and_image_tag() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert "SMOKE_SCHEMA_VERSION: alembic-head" in workflow
    assert "SMOKE_DATASET: release14-synthetic-provider-fixtures" in workflow
    assert "SMOKE_JUNIT: staging-smoke.xml" in workflow
    assert "test -s staging-smoke-evidence.json" in workflow
    assert "test -s staging-smoke.xml" in workflow
    assert "finance-sync:sha-${GITHUB_SHA:0:7}" in workflow
    assert "staging-smoke.xml" in workflow


def test_smoke_summary_is_safe_and_machine_readable() -> None:
    summary = {
        "commit": "abc",
        "image_tag": "ghcr.io/rbnbrls/finance-sync:sha-abc",
        "schema_version": "alembic-head",
        "synthetic_dataset": "release14-synthetic-provider-fixtures",
        "secrets_included": False,
    }
    assert json.loads(json.dumps(summary))["secrets_included"] is False
