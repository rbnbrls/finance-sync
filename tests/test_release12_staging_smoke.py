"""Contract tests for staging smoke, evidence and rollback policy."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SMOKE = PROJECT_ROOT / "scripts/release_smoke.py"
RELEASE = PROJECT_ROOT / ".github/workflows/release.yml"
README = PROJECT_ROOT / "README.md"
ARCHITECTURE = PROJECT_ROOT / "docs/ARCHITECTURE.md"
DATABASE = PROJECT_ROOT / "docs/DATABASE.md"
UPGRADE = PROJECT_ROOT / "docs/UPGRADE.md"
RELEASING = PROJECT_ROOT / "docs/RELEASING.md"


def test_smoke_covers_synthetic_sync_outbox_and_exporter() -> None:
    smoke = SMOKE.read_text(encoding="utf-8")
    for endpoint in (
        "/health/live",
        "/health/ready",
        "/api/v1/sync/bunq",
        "/api/v1/sync-runs",
        "/api/v1/exporters/export",
        "/api/v1/exporters/runs",
    ):
        assert endpoint in smoke
    assert '"synthetic_data_only": True' in smoke
    assert '"secrets_included": False' in smoke


def test_release_smoke_uploads_commit_bound_evidence() -> None:
    workflow = RELEASE.read_text(encoding="utf-8")
    assert "SMOKE_ARTIFACT: staging-smoke-evidence.json" in workflow
    assert "SMOKE_COMMIT: ${{ github.sha }}" in workflow
    assert "SMOKE_IMAGE_TAG: ghcr.io/rbnbrls/finance-sync:sha-${{ github.sha }}" in workflow
    assert "release-staging-smoke-${{ github.sha }}" in workflow
    assert "needs: deploy-staging" in workflow


def test_release_docs_define_image_rollback_without_production_downgrade() -> (
    None
):
    for path in (README, ARCHITECTURE, DATABASE, UPGRADE, RELEASING):
        text = path.read_text(encoding="utf-8").lower()
        assert "backward-compatible" in text or "backwards-compatible" in text
        assert "image rollback" in text or "application-image rollback" in text
    releasing = RELEASING.read_text(encoding="utf-8").lower()
    assert "never a blind schema downgrade" in releasing
