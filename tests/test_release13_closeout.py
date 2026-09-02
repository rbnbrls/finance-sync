"""Release 13 closeout contract tests."""

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_pyright_baseline_and_ci_type_gates_are_ratcheted() -> None:
    budget = json.loads(
        (ROOT / "config/pyright-warning-budget.json").read_text(
            encoding="utf-8"
        )
    )
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert budget["max_warnings"] <= 60
    assert "run: make type" in workflow
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "uv run pyright -p pyproject.toml src" in makefile
    assert "uv run pyright -p pyrightconfig.tests.json tests" in makefile
    assert "check_pyright_budget.py" in workflow


def test_release_docs_contain_staging_rollback_and_evidence_checklist() -> None:
    releasing = (ROOT / "docs/RELEASING.md").read_text(encoding="utf-8")
    assert "Release evidence checklist" in releasing
    for field in (
        "Commit SHA",
        "Immutable image tag",
        "Owner",
        "Verification date",
    ):
        assert field in releasing
    assert "security-evidence" in releasing
    assert "image rollback" in releasing

    for name in (
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/DATABASE.md",
        "docs/UPGRADE.md",
    ):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        assert "release 13" in text
