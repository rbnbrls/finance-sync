"""Release 16 dependency-upgrade cadence contracts."""

# pyright: basic

from pathlib import Path


def test_dependabot_defines_owned_weekly_uv_updates() -> None:
    config = Path(".github/dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: uv" in config
    assert "interval: weekly" in config
    assert "security" in config


def test_dependency_workflow_runs_all_compatibility_gates() -> None:
    workflow = Path(".github/workflows/dependency-cadence.yml").read_text(
        encoding="utf-8"
    )
    for marker in (
        "uv lock --check",
        "Unit gate",
        "Integration gate",
        "E2E gate",
        "pip-audit",
        "cyclonedx",
    ):
        assert marker in workflow


def test_release_promotion_keeps_security_and_runtime_gates() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "security-evidence" in workflow
    assert "operational-summary" in workflow
    assert "needs: operational-summary" in workflow
