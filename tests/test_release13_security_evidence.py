"""Release 13 contract tests for security scan evidence."""

import json
from pathlib import Path

import pytest

from scripts.check_security_evidence import validate

ROOT = Path(__file__).parents[1]


def test_release_workflow_gates_and_uploads_all_security_evidence() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert "security-evidence:" in workflow
    assert "pip-audit.json" in workflow
    assert "sbom.cyclonedx.json" in workflow
    assert "release-trivy-${{ github.sha }}" in workflow
    assert "check_trivyignore.py" in workflow
    assert "check_security_evidence.py" in workflow
    assert "name: release-security-evidence" in workflow
    assert "security-evidence" in workflow.split("deploy-staging:", 1)[1]


def test_security_evidence_rejects_credentials(tmp_path: Path) -> None:
    safe = tmp_path / "safe.json"
    safe.write_text(json.dumps({"components": [{"name": "pydantic"}]}))
    validate([safe])

    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text(json.dumps({"api_token": "do-not-publish"}))
    with pytest.raises(ValueError, match="sensitive values"):
        validate([unsafe])
