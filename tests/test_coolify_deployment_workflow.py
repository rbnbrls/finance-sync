"""Contract tests for the production Coolify deployment target."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = PROJECT_ROOT / ".github" / "workflows" / "deploy.yml"
RELEASE = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
PRODUCTION_APP_UUID = "xnenwsyifmv81xkvdxkbkx3a"
STALE_APP_UUID = "5iov9i8w9mazc6vhismo3enx"


def test_deploy_workflow_targets_current_production_app() -> None:
    workflow = DEPLOY.read_text(encoding="utf-8")

    assert PRODUCTION_APP_UUID in workflow
    assert STALE_APP_UUID not in workflow
    assert '"uuid": "xnenwsyifmv81xkvdxkbkx3a"' in workflow
    assert "https://finance-sync.7rb.nl/health/live" in workflow
    assert "https://finance-sync.7rb.nl/health/ready" in workflow


def test_release_promotion_uses_current_production_app() -> None:
    workflow = RELEASE.read_text(encoding="utf-8")

    assert f"PROD_APP_UUID: {PRODUCTION_APP_UUID}" in workflow
    assert STALE_APP_UUID not in workflow
