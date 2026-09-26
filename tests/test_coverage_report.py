"""Published coverage-report contracts.

The darkfactory quality lane measured this repository on `main` (2026-09-26)
and reported the coverage rung as `enforced` with
`capability:coverage_not_published`: CI runs the unit suite with an 80%
threshold, but no machine-readable report was readable from the default branch
(`coverage.xml` was git-ignored and only uploaded as an expiring CI artifact).
The lane reads a committed report, so the measurement a reader can trust has to
live in the repository.

These tests pin the four things that make the publication real rather than
decorative:

* the report is committed at the repository root and parses to a percentage
  (what moves the lane's rung from `enforced` to `measured`);
* it is machine-independent -- no run timestamp, no absolute checkout path --
  because a report that only reproduces on the machine that wrote it cannot be
  checked by anyone else;
* CI re-measures, republishes and refuses a stale committed report, so the
  published number cannot silently drift from the code;
* CI publishes *through the merge*, not by committing from the workflow: the
  workflow token has no write grant here.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts.coverage_report import normalise_report, parse_report

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_REPORT = REPO_ROOT / "coverage.xml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"
GITIGNORE = REPO_ROOT / ".gitignore"

_COBERTURA_FIXTURE = """<?xml version="1.0" ?>
<coverage version="7.15.2" timestamp="1790442691766" lines-valid="10" lines-covered="7" line-rate="0.7" branches-valid="4" branches-covered="2" branch-rate="0.5" complexity="0">
\t<sources>
\t\t<source>/home/runner/work/finance-sync/finance-sync</source>
\t</sources>
\t<packages>
\t\t<package name="src.finance_sync" line-rate="0.7" branch-rate="0.5" complexity="0">
\t\t\t<classes>
\t\t\t\t<class name="app.py" filename="src/finance_sync/app.py" complexity="0" line-rate="0.7" branch-rate="0.5">
\t\t\t\t\t<lines>
\t\t\t\t\t\t<line number="1" hits="1"/>
\t\t\t\t\t</lines>
\t\t\t\t</class>
\t\t\t</classes>
\t\t</package>
\t</packages>
</coverage>
"""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_published_report_is_committed_at_the_repository_root() -> None:
    assert PUBLISHED_REPORT.is_file(), (
        "coverage.xml must be committed at the repository root: the quality "
        "lane reads a committed report, never an expiring CI artifact"
    )
    tracked = _git("ls-files", "--error-unmatch", "coverage.xml")
    assert tracked.returncode == 0, "coverage.xml is not tracked by git"
    ignored = _git("check-ignore", "--quiet", "coverage.xml")
    assert ignored.returncode != 0, (
        "coverage.xml is git-ignored and cannot be read"
    )


def test_published_report_parses_to_a_percentage() -> None:
    summary = parse_report(PUBLISHED_REPORT)
    assert summary.lines_valid > 0
    assert 0 < summary.lines_covered <= summary.lines_valid
    assert 0.0 < summary.line_percent <= 100.0
    # `line-rate` is the field the quality lane parses, and it must agree with
    # the line totals the report also carries.
    expected = summary.lines_covered / summary.lines_valid
    assert abs(summary.line_rate - expected) < 1e-3


def test_published_report_is_machine_independent() -> None:
    text = PUBLISHED_REPORT.read_text(encoding="utf-8")
    assert "timestamp=" not in text
    assert 'filename="/' not in text
    assert "<source>.</source>" in text
    # The published report describes the checked-out files it measured.
    for class_element in ElementTree.fromstring(text).iter("class"):
        filename = class_element.attrib["filename"]
        assert not filename.startswith("/")
        assert (REPO_ROOT / filename).is_file(), (
            f"{filename} is not in the tree"
        )


def test_normalising_removes_volatile_fields_and_is_idempotent(
    tmp_path: Path,
) -> None:
    report = tmp_path / "coverage.xml"
    report.write_text(_COBERTURA_FIXTURE, encoding="utf-8")

    summary = normalise_report(report)

    assert summary.line_percent == 70.0
    assert summary.lines_covered == 7
    assert summary.lines_valid == 10
    assert summary.branches_valid == 4
    published = report.read_text(encoding="utf-8")
    assert "timestamp=" not in published
    assert "/home/runner" not in published
    assert "<source>.</source>" in published
    assert published.endswith("\n")

    # Re-normalising the published copy is a no-op: the CI staleness gate
    # compares bytes, so an unstable normaliser would fail on a clean run.
    normalise_report(report)
    assert report.read_text(encoding="utf-8") == published


def test_normalising_writes_the_published_copy(tmp_path: Path) -> None:
    source = tmp_path / "raw.xml"
    source.write_text(_COBERTURA_FIXTURE, encoding="utf-8")
    destination = tmp_path / "nested" / "coverage.xml"

    normalise_report(source, destination)

    assert destination.is_file()
    assert "timestamp=" not in destination.read_text(encoding="utf-8")


def test_unreadable_reports_are_errors_not_percentages(tmp_path: Path) -> None:
    missing = tmp_path / "absent.xml"
    with pytest.raises(ValueError):
        parse_report(missing)

    fragment = tmp_path / "fragment.xml"
    fragment.write_text("<coverage></coverage>", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_report(fragment)

    not_a_number = tmp_path / "odd.xml"
    not_a_number.write_text(
        '<coverage line-rate="lots"></coverage>', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        parse_report(not_a_number)


def test_ci_publishes_and_guards_the_committed_report() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    measure = workflow.index("run: make test-ci")
    publish = workflow.index("run: make coverage-publish")
    guard = workflow.index("run: make coverage-check")
    assert measure < publish < guard, (
        "the test job must measure, publish and then refuse a stale report"
    )
    # The uploaded artifact is the published file, not a second report.
    assert "name: coverage-report-" in workflow
    assert "make coverage-refresh" in workflow


def test_ci_does_not_write_back_to_the_repository() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "contents: write" not in workflow
    assert "git commit" not in workflow
    assert "git push" not in workflow


def test_quality_gate_and_makefile_carry_the_report_contract() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert "coverage-publish:" in makefile
    assert "coverage-check:" in makefile
    # Local `make ci-fast` runs the same sequence the workflow runs: measure,
    # publish, then refuse a stale report.
    assert "test-ci coverage-publish coverage-check" in makefile
    # The published report is source, not build output: `make clean` must not
    # delete it.
    assert ".coverage junit.xml" in makefile
    assert "coverage.xml junit.xml" not in makefile


def test_coverage_configuration_publishes_a_relative_report() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    run_config = config["tool"]["coverage"]["run"]
    assert run_config["relative_files"] is True
    assert config["tool"]["coverage"]["xml"]["output"] == "coverage.xml"
    assert config["tool"]["coverage"]["report"]["fail_under"] == 80


def test_the_ignore_file_does_not_hide_the_published_report() -> None:
    lines = [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
    ]
    assert "coverage.xml" not in lines
    assert ".coverage" in lines
