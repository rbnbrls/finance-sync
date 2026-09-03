"""Regression tests for production image runtime dependencies."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERFILE_WORKER = PROJECT_ROOT / "Dockerfile.worker"


def _production_stage(path: Path) -> str:
    """Return Dockerfile text from the production stage onwards."""
    dockerfile = path.read_text()
    marker = "AS production"
    assert marker in dockerfile, (
        f"production stage marker not found in {path.name}"
    )
    return dockerfile.split(marker, 1)[1]


def test_production_stage_installs_healthcheck_tools() -> None:
    """Both production images provide tools used by healthcheck probes."""
    for path in (DOCKERFILE, DOCKERFILE_WORKER):
        stage = _production_stage(path)
        assert "curl" in stage, f"curl missing from {path.name}"
        assert "wget" in stage, f"wget missing from {path.name}"


def test_production_stage_removes_unneeded_systemd_libraries() -> None:
    """Images must not ship the vulnerable, unused systemd runtime libraries."""
    for path in (DOCKERFILE, DOCKERFILE_WORKER):
        stage = _production_stage(path)
        assert "apt-get purge" in stage, (
            f"systemd cleanup missing from {path.name}"
        )
        assert "libsystemd0" in stage, (
            f"libsystemd0 cleanup missing from {path.name}"
        )
        assert "libudev1" in stage, f"libudev1 cleanup missing from {path.name}"


def test_production_stage_has_healthcheck() -> None:
    """Each production image must declare a live healthcheck."""
    for path in (DOCKERFILE, DOCKERFILE_WORKER):
        stage = _production_stage(path)
        assert "HEALTHCHECK" in stage, f"healthcheck missing from {path.name}"
        assert "/health/live" in stage, (
            f"healthcheck target missing from {path.name}"
        )
