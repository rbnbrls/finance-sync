"""Release 14 backlog closeout audit contract.

Regression coverage: CI #723 (Test 3.12) failed because commit 9a989fa
deleted the four release-14 story files.  audit() then crashed with
FileNotFoundError inside path.read_text() instead of reporting a clean
audit finding, so the failure surfaced as an unhandled exception rather
than as an actionable "missing story file" error.
"""

from pathlib import Path

import pytest

from scripts.check_release14_backlog import STORIES, audit

ROOT = Path(__file__).parents[1]


def test_release14_stories_are_done_and_auditable() -> None:
    assert audit() == []
    for filename in STORIES:
        text = (ROOT / "backlog" / filename).read_text(encoding="utf-8")
        assert "tests/" in text
        assert "CI" in text or "artifact" in text


def test_release14_story_files_exist() -> None:
    """The release-14 closeout audit contract requires all story files to exist.

    Regression guard: commit 9a989fa deleted these files, which made
    test_release14_stories_are_done_and_auditable raise FileNotFoundError
    (CI #719). The audit contract must fail loudly on missing stories
    instead of crashing on a read.
    """
    for filename in STORIES:
        path = ROOT / "backlog" / filename
        assert path.is_file(), f"missing release-14 story file: {path}"


def test_audit_reports_missing_story_file_as_finding_not_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing story files must yield a clean audit finding, never a crash.

    Regression guard for CI #723: when a release-14 story file is deleted
    the audit must report ``<filename>: missing story file`` instead of
    raising FileNotFoundError from path.read_text(). This keeps the
    failure mode actionable (and the closeout test green on the restored
    tree) for anyone auditing the backlog from a partially-merged tree.
    """
    from scripts import check_release14_backlog as mod

    # Recreate the CI #723 condition: story files deleted from the tree.
    # Point the audit at an empty temp dir so every story is missing.
    monkeypatch.setattr(mod, "BACKLOG", tmp_path)

    findings = audit()
    assert findings == [
        f"{filename}: missing story file" for filename in STORIES
    ]


def test_backlog_convention_remains_unchanged() -> None:
    readme = (ROOT / "backlog/README.md").read_text(encoding="utf-8")
    assert (
        "status: todo        # todo | in-progress | done | cancelled" in readme
    )
    assert "higher = sooner" in readme
