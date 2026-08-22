"""Contract tests for mandatory release-candidate CI gates."""

from pathlib import Path


RELEASE_WORKFLOW = Path(__file__).parents[1] / ".github/workflows/release.yml"


def test_release_workflow_has_mandatory_service_gates() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "release-gates:" in workflow
    assert "image: postgres:16" in workflow
    assert "image: redis:7" in workflow
    assert "pytest -m integration" in workflow
    assert "pytest -m e2e" in workflow
    assert "check_junit_no_skips.py junit-integration.xml" in workflow
    assert "check_junit_no_skips.py junit-e2e.xml" in workflow
    assert "release-gates" in workflow


def test_release_workflow_publishes_migration_round_trip() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "uv run alembic downgrade base" in workflow
    assert "uv run alembic upgrade head" in workflow
    assert "name: release-migration-round-trip" in workflow
    assert "migration.log" in workflow
