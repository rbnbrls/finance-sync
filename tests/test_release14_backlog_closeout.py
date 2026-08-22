"""Release 14 backlog closeout audit contract."""

from pathlib import Path

from scripts.check_release14_backlog import STORIES, audit

ROOT = Path(__file__).parents[1]


def test_release14_stories_are_done_and_auditable() -> None:
    assert audit() == []
    for filename in STORIES:
        text = (ROOT / "backlog" / filename).read_text(encoding="utf-8")
        assert "tests/" in text
        assert "CI" in text or "artifact" in text


def test_backlog_convention_remains_unchanged() -> None:
    readme = (ROOT / "backlog/README.md").read_text(encoding="utf-8")
    assert (
        "status: todo        # todo | in-progress | done | cancelled" in readme
    )
    assert "higher = sooner" in readme
