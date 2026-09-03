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


def test_production_stage_uses_runtime_without_systemd_libraries() -> None:
    """Runtime bases must not inherit the vulnerable systemd libraries."""
    for path in (DOCKERFILE, DOCKERFILE_WORKER):
        dockerfile = path.read_text()
        stage = _production_stage(path)
        assert "FROM python:3.12-alpine AS production" in dockerfile, (
            f"systemd-free runtime base missing from {path.name}"
        )
        assert "apt-get purge" not in stage, (
            f"runtime must not remove essential packages in {path.name}"
        )
        assert "apk add" in stage, (
            f"Alpine runtime packages missing from {path.name}"
        )


def test_production_stage_has_healthcheck() -> None:
    """Each production image must declare a live healthcheck."""
    for path in (DOCKERFILE, DOCKERFILE_WORKER):
        stage = _production_stage(path)
        assert "HEALTHCHECK" in stage, f"healthcheck missing from {path.name}"
        assert "/health/live" in stage, (
            f"healthcheck target missing from {path.name}"
        )
