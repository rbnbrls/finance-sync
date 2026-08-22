"""Tests for the required-service-gate JUnit skip check."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.check_junit_no_skips import count_skips

if TYPE_CHECKING:
    from pathlib import Path


def test_count_skips_accepts_a_report_without_skips(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="1" failures="0"><testcase /></testsuite>',
        encoding="utf-8",
    )

    assert count_skips(report) == 0


def test_count_skips_detects_skipped_cases(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="2" skipped="1">'
        "<testcase /><testcase><skipped /></testcase></testsuite>",
        encoding="utf-8",
    )

    assert count_skips(report) == 1
