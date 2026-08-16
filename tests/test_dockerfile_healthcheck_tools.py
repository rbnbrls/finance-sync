"""Regression test: the production image must ship both curl and wget.

Issue #233 (finance-sync-staging crash/restart): Coolify manages the
app-level healthcheck itself (custom_healthcheck_found=false) and injects
a **wget**-based probe.  The production image shipped curl only, so every
probe failed with "/bin/sh: 1: wget: not found" (rc 1) and Coolify rolled
back every deployment ("New container is not healthy, rolling back...").

The fix pins BOTH tools in the production stage of the Dockerfile:
  - curl  → backs the Dockerfile HEALTHCHECK below
  - wget  → satisfies Coolify's default injected probe

This test asserts that coupling so a future Dockerfile edit cannot
silently drop either tool again.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"


def _production_stage() -> str:
    """Return the Dockerfile text from the production stage onwards."""
    dockerfile = DOCKERFILE.read_text()
    marker = "AS production"
    assert marker in dockerfile, (
        "production stage marker not found in Dockerfile"
    )
    return dockerfile.split(marker, 1)[1]


def test_production_stage_installs_curl() -> None:
    """The Dockerfile HEALTHCHECK depends on curl being present."""
    stage = _production_stage()
    assert "curl" in stage, (
        "curl missing from production stage (HEALTHCHECK depends on it)"
    )


def test_production_stage_installs_wget() -> None:
    """Coolify's default healthcheck probe is wget-based (issue #233)."""
    stage = _production_stage()
    assert "wget" in stage, (
        "wget missing from production stage (Coolify default probe needs it)"
    )


def test_production_stage_has_healthcheck() -> None:
    """The image must keep declaring a HEALTHCHECK on /health/live."""
    stage = _production_stage()
    assert "HEALTHCHECK" in stage, "production stage has no HEALTHCHECK"
    assert "/health/live" in stage, "HEALTHCHECK must target /health/live"
