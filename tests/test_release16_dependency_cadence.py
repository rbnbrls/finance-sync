"""Release 16 dependency-upgrade cadence contracts."""

# pyright: basic

import tomllib
from pathlib import Path


def test_dependabot_defines_owned_weekly_uv_updates() -> None:
    config = Path(".github/dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: uv" in config
    assert "interval: weekly" in config
    assert "security" in config


def test_dependabot_update_set_is_represented_in_lockfile() -> None:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    packages = {
        package["name"]: package["version"] for package in lock["package"]
    }
    assert packages["actualpy"] == "0.22.4"
    assert packages["cryptography"] == "50.0.2"
    assert packages["sentry-sdk"] == "2.71.0"
    assert packages["sse-starlette"] == "3.5.0"


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
