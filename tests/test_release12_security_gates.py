"""Contract tests for Release 12 dependency and image security gates."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts.check_trivyignore import parse_entries, validate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = PROJECT_ROOT / ".github/workflows/ci.yml"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github/workflows/release.yml"
TRIVYIGNORE = PROJECT_ROOT / ".trivyignore"


def test_trivy_baseline_is_unique_bounded_and_has_rationale() -> None:
    entries = parse_entries(TRIVYIGNORE)
    validate(TRIVYIGNORE, today=date(2026, 8, 21))
    assert entries
    assert len({finding for finding, _, _ in entries}) == len(entries)
    assert all(expiry and rationale for _, expiry, rationale in entries)


def test_expired_trivy_entry_fails_the_gate(tmp_path: Path) -> None:
    baseline = tmp_path / ".trivyignore"
    baseline.write_text(
        '# rationale\n# expiry="2026-01-01"\nCVE-TEST\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expired"):
        validate(baseline, today=date(2026, 8, 21))


def test_ci_audits_locked_release_requirements_and_publishes_sbom() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "uv export --format requirements-txt" in workflow
    assert "--no-dev" in workflow
    assert "--locked" in workflow
    assert "pip-audit --requirement release-requirements.txt" in workflow
    assert "cyclonedx-py requirements release-requirements.txt" in workflow
    assert "name: sbom-${{ github.sha }}" in workflow


def test_image_scan_is_fail_closed_and_publishes_provenance() -> None:
    for workflow_path in (CI_WORKFLOW, RELEASE_WORKFLOW):
        workflow = workflow_path.read_text(encoding="utf-8")
        assert "severity: HIGH,CRITICAL" in workflow_path.read_text(
            encoding="utf-8"
        )
        assert "exit-code: 1" in workflow
        assert "trivyignores: .trivyignore" in workflow
        assert "format: json" in workflow
        assert "output: trivy-results.json" in workflow
        assert "COMMIT_SHA: ${{ github.sha }}" in workflow
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "name: trivy-${{ github.sha }}" in ci
    release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "name: release-trivy-${{ github.sha }}" in release
