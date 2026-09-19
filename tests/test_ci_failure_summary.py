from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.ci_failure_summary import build_summary

if TYPE_CHECKING:
    from pathlib import Path


def test_build_summary_extracts_first_junit_failure(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<testsuite><testcase classname="pkg.test" name="test_bad">
        <error message="ImportError">E   missing helper\nsecond line</error>
        </testcase></testsuite>""",
        encoding="utf-8",
    )

    summary = build_summary(junit, None, "Test failure")

    assert "pkg.test.test_bad: E   missing helper" in summary
    assert "second line" not in summary


def test_build_summary_falls_back_to_log(tmp_path: Path) -> None:
    junit = tmp_path / "missing.xml"
    log = tmp_path / "test.log"
    log.write_text(
        "collecting\nE   ModuleNotFoundError: missing\n", encoding="utf-8"
    )

    summary = build_summary(junit, log, "Collection failure")

    assert "ModuleNotFoundError" in summary
