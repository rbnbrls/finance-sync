"""Published coverage-summary contracts.

The darkfactory quality lane measured this repository on `main` (2026-09-26)
and reported the coverage rung as `enforced` with
`capability:coverage_not_published`: CI runs the unit suite with an 80%
threshold, but nothing readable lived on the default branch. The first repair
(PR #1004) committed the per-line Cobertura report itself, and the lane still
reported the same gap: it reads a committed file through the provider's
contents API, which answers with an *empty body* above 1MB, and the report is
1.5MB -- published and invisible at the same time.

These tests pin what makes the publication real rather than decorative:

* the committed summary exists at the repository root, is tracked, and parses
  to a percentage through the same `total.lines.pct` field the lane reads
  (that is what moves the rung from `enforced` to `measured`);
* it stays inside the reader's limits -- that constraint is *why* the published
  artifact is a summary, so a future change that commits the full report again
  has to argue with a test, not with a comment;
* it is machine-independent and a pure function of the measurement, so CI can
  re-publish it and fail on a stale copy;
* publishing never invents a number: per-file counters that do not sum to the
  reporter's own totals are an error, and an unreadable report is an error
  rather than a zero;
* CI publishes *through the merge*: the workflow token has no write grant.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts.coverage_report import parse_summary, read_report, write_summary

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_SUMMARY = REPO_ROOT / "coverage-summary.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"
GITIGNORE = REPO_ROOT / ".gitignore"

#: The lane truncates a fetched file at 400,000 characters; the provider's
#: contents API returns an empty body above 1MB. The published artifact has to
#: fit the smaller of the two with room to spare.
_READER_LIMIT = 400_000

_COBERTURA_FIXTURE = """<?xml version="1.0" ?>
<coverage version="7.15.2" timestamp="1790442691766" lines-valid="5" lines-covered="3" line-rate="0.6" branches-valid="4" branches-covered="3" branch-rate="0.75" complexity="0">
\t<sources>
\t\t<source>/home/runner/work/finance-sync/finance-sync</source>
\t</sources>
\t<packages>
\t\t<package name="src.finance_sync" line-rate="0.6" branch-rate="0.75" complexity="0">
\t\t\t<classes>
\t\t\t\t<class name="a.py" filename="src/finance_sync/a.py" complexity="0" line-rate="0.6667" branch-rate="0.5">
\t\t\t\t\t<lines>
\t\t\t\t\t\t<line number="1" hits="1"/>
\t\t\t\t\t\t<line number="2" hits="0" branch="true" condition-coverage="50% (1/2)"/>
\t\t\t\t\t\t<line number="3" hits="1"/>
\t\t\t\t\t</lines>
\t\t\t\t</class>
\t\t\t\t<class name="b.py" filename="src/finance_sync/b.py" complexity="0" line-rate="0.5" branch-rate="1">
\t\t\t\t\t<lines>
\t\t\t\t\t\t<line number="1" hits="0"/>
\t\t\t\t\t\t<line number="2" hits="1" branch="true" condition-coverage="100% (2/2)"/>
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


def _fixture(tmp_path: Path, text: str = _COBERTURA_FIXTURE) -> Path:
    report = tmp_path / "coverage.xml"
    report.write_text(text, encoding="utf-8")
    return report


def test_published_summary_is_committed_at_the_repository_root() -> None:
    assert PUBLISHED_SUMMARY.is_file(), (
        "coverage-summary.json must be committed at the repository root: the "
        "quality lane reads a committed file, never an expiring CI artifact"
    )
    tracked = _git("ls-files", "--error-unmatch", "coverage-summary.json")
    assert tracked.returncode == 0, (
        "coverage-summary.json is not tracked by git"
    )
    ignored = _git("check-ignore", "--quiet", "coverage-summary.json")
    assert ignored.returncode != 0, (
        "coverage-summary.json is git-ignored and cannot be read"
    )


def test_published_summary_parses_to_a_percentage() -> None:
    published = parse_summary(PUBLISHED_SUMMARY)
    assert published.lines_valid > 0
    assert 0 < published.lines_covered <= published.lines_valid
    assert 0.0 < published.line_percent <= 100.0
    assert published.files, "the summary must name the files it measured"
    for name in published.files:
        assert not name.startswith("/")
        assert (REPO_ROOT / name).is_file(), f"{name} is not in the tree"


def test_published_summary_fits_the_readers_limits() -> None:
    size = len(PUBLISHED_SUMMARY.read_bytes())
    assert size < _READER_LIMIT, (
        "the published summary must stay inside the reader's limit "
        f"({_READER_LIMIT} characters); {size} bytes is the size that made the "
        "per-line report unreadable"
    )


def test_the_per_line_report_is_build_output_not_the_published_artifact() -> (
    None
):
    # The report stays available (workflow artifact + local runs); it is simply
    # too large for the lane's reader, so it is not the committed number.
    ignored = _git("check-ignore", "--quiet", "coverage.xml")
    assert ignored.returncode == 0, (
        "coverage.xml is a build output; committing it publishes a number the "
        "lane cannot fetch"
    )


def test_publishing_is_deterministic(tmp_path: Path) -> None:
    report = _fixture(tmp_path)
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"

    summary = write_summary(report, first)
    write_summary(report, second)

    assert summary.line_percent == 60.0
    assert summary.lines.covered == 3
    assert summary.lines.total == 5
    assert summary.branches.covered == 3
    assert summary.branches.total == 4
    assert first.read_bytes() == second.read_bytes()
    assert first.read_text(encoding="utf-8").endswith("\n")
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["total"]["lines"]["pct"] == 60.0
    assert payload["total"]["branches"]["pct"] == 75.0
    assert list(payload["files"]) == [
        "src/finance_sync/a.py",
        "src/finance_sync/b.py",
    ]
    # No run timestamp and no checkout path: the summary is a function of what
    # the tests executed, so CI can compare bytes.
    assert "timestamp" not in payload
    assert "/home/runner" not in first.read_text(encoding="utf-8")


def test_publishing_rejects_a_report_that_contradicts_its_own_totals(
    tmp_path: Path,
) -> None:
    contradictory = _COBERTURA_FIXTURE.replace(
        'lines-valid="5"', 'lines-valid="9"'
    )
    report = _fixture(tmp_path, contradictory)

    with pytest.raises(ValueError, match="do not sum"):
        write_summary(report, tmp_path / "summary.json")

    assert not (tmp_path / "summary.json").exists()


def test_unreadable_reports_are_errors_not_percentages(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_report(tmp_path / "absent.xml")

    fragment = tmp_path / "fragment.xml"
    fragment.write_text("<coverage></coverage>", encoding="utf-8")
    with pytest.raises(ValueError):
        read_report(fragment)

    not_xml = tmp_path / "notes.txt"
    not_xml.write_text("no report here", encoding="utf-8")
    with pytest.raises(ValueError):
        read_report(not_xml)

    measured_nothing = tmp_path / "empty.xml"
    measured_nothing.write_text(
        '<coverage lines-valid="0" lines-covered="0" line-rate="0"></coverage>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        read_report(measured_nothing)


def test_unreadable_summaries_are_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        parse_summary(tmp_path / "absent.json")

    shape = tmp_path / "shape.json"
    shape.write_text('{"files": {"src/a.py": {}}}', encoding="utf-8")
    with pytest.raises(ValueError):
        parse_summary(shape)

    no_files = tmp_path / "no-files.json"
    no_files.write_text(
        '{"total": {"lines": {"pct": 60.0, "total": 5, "covered": 3}}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        parse_summary(no_files)


def test_ci_publishes_and_guards_the_committed_summary() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    measure = workflow.index("run: make test-ci")
    publish = workflow.index("run: make coverage-publish")
    guard = workflow.index("run: make coverage-check")
    assert measure < publish < guard, (
        "the test job must measure, publish and then refuse a stale summary"
    )
    # Both halves of the measurement travel: the summary is the committed
    # number, the per-line report is the artifact.
    assert "name: coverage-report-" in workflow
    assert "coverage-summary.json" in workflow
    assert "make coverage-refresh" in workflow


def test_ci_does_not_write_back_to_the_repository() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "contents: write" not in workflow
    assert "git commit" not in workflow
    assert "git push" not in workflow


def _make_target(makefile: str, target: str) -> str:
    """Return the recipe lines of one Makefile target."""

    lines = makefile.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith(f"{target}:")
    )
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("\t"):
            block.append(line)
            continue
        break
    return "\n".join(block)


def test_quality_gate_and_makefile_carry_the_summary_contract() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert "coverage-publish:" in makefile
    assert "coverage-check:" in makefile
    # Local `make ci-fast` runs the same sequence the workflow runs: measure,
    # publish, then refuse a stale summary.
    assert "test-ci coverage-publish coverage-check" in makefile
    # A refusal has to say *why*: the gate must not answer an interpreter
    # mismatch with "refresh and commit", because following that advice is what
    # publishes the artifact CI rejects.
    assert "scripts/coverage_report.py --compare HEAD" in makefile
    # The committed summary is source, not build output: `make clean` must not
    # delete it, while the per-line report stays disposable.
    clean = _make_target(makefile, "clean")
    assert "coverage-summary.json" not in clean
    assert "coverage.xml" in clean


def test_coverage_configuration_publishes_relative_paths() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    run_config = config["tool"]["coverage"]["run"]
    assert run_config["relative_files"] is True
    assert config["tool"]["coverage"]["xml"]["output"] == "coverage.xml"
    assert config["tool"]["coverage"]["report"]["fail_under"] == 80


def test_the_ignore_file_hides_the_build_output_only() -> None:
    lines = [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
    ]
    assert "coverage.xml" in lines
    assert "coverage-summary.json" not in lines
    assert ".coverage" in lines


def test_published_summary_matches_its_own_per_file_detail() -> None:
    """The committed file is internally consistent, not just parseable."""

    payload = json.loads(PUBLISHED_SUMMARY.read_text(encoding="utf-8"))
    total_lines = sum(
        entry["lines"]["total"] for entry in payload["files"].values()
    )
    covered_lines = sum(
        entry["lines"]["covered"] for entry in payload["files"].values()
    )
    assert total_lines == payload["total"]["lines"]["total"]
    assert covered_lines == payload["total"]["lines"]["covered"]


# --- the summary is only reproducible on one interpreter -------------------- #


def _summary(files: dict[str, tuple[int, int]]) -> dict[str, object]:
    """A minimal summary payload: ``{path: (statements, covered)}``."""
    return {
        "total": {
            "lines": {"total": 0, "covered": 0, "skipped": 0, "pct": 0.0}
        },
        "files": {
            name: {
                "lines": {
                    "total": total,
                    "covered": covered,
                    "skipped": 0,
                    "pct": 0.0,
                },
                "branches": {
                    "total": 0,
                    "covered": 0,
                    "skipped": 0,
                    "pct": 0.0,
                },
            }
            for name, (total, covered) in files.items()
        },
    }


def test_the_measuring_interpreter_is_pinned_to_the_one_ci_installs() -> None:
    """`uv run` must resolve the interpreter the workflow measures with.

    coverage.py derives a file's statement total from the parse of the running
    interpreter, so the same tree measured by two interpreters yields two
    published summaries: `auth.py` is 135 statements under 3.14 and 164 under
    3.12. Without the pin, a local `make ci-fast` publishes the summary the 3.12
    CI job rejects on every push -- and reports success while doing it, because
    the same wrong measurement is on both sides of the local gate.
    """

    pin = REPO_ROOT / ".python-version"
    assert pin.is_file(), "the measurement interpreter must be pinned for uv"
    pinned = pin.read_text(encoding="utf-8").strip()
    assert pinned, ".python-version must not be empty"

    # The pin is only meaningful if it names the interpreter CI measures with.
    workflow = WORKFLOW.read_text(encoding="utf-8").replace("'", '"')
    assert f'python-version: ["{pinned}"]' in workflow, (
        "the pinned interpreter must be the one the test job installs"
    )

    # And it only takes effect because the local gates run through `uv run`,
    # which resolves `.python-version`; a bare `python3` would ignore it.
    makefile = MAKEFILE.read_text(encoding="utf-8")
    for target in ("test-ci", "coverage-publish", "coverage-check"):
        recipe = _make_target(makefile, target)
        assert "uv run python" in recipe or "uv run pytest" in recipe, (
            f"{target} must resolve the pinned interpreter through uv"
        )
    # `ci-fast` is the gate the factory runs; it must compose those targets.
    assert "test-ci coverage-publish coverage-check" in _make_target(
        makefile, "ci-fast"
    )


def test_a_statement_total_divergence_is_not_reported_as_stale() -> None:
    """The one divergence no local refresh can fix must not say "refresh"."""

    from scripts.coverage_report import (
        DIVERGENCE_STATEMENTS,
        compare_summaries,
        diagnose,
    )

    published = _summary({"src/finance_sync/a.py": (135, 90)})
    regenerated = _summary({"src/finance_sync/a.py": (164, 110)})

    divergence = compare_summaries(published, regenerated)

    assert divergence.kind == DIVERGENCE_STATEMENTS
    assert divergence.statements == 1
    assert "a.py (135 -> 164)" in divergence.examples[0]

    text = "\n".join(diagnose(divergence, measured_files=1))
    assert "statement totals" in text
    assert "not measured by the same interpreter" in text
    # The load-bearing sentence: the old remedy is wrong for this class.
    assert "Refreshing here cannot converge" in text
    assert "coverage-refresh" not in text


def test_a_coverage_only_divergence_is_still_reported_as_stale() -> None:
    """A real coverage movement keeps the ordinary remedy."""

    from scripts.coverage_report import (
        DIVERGENCE_COVERAGE,
        compare_summaries,
        diagnose,
    )

    published = _summary({"src/finance_sync/a.py": (135, 90)})
    regenerated = _summary({"src/finance_sync/a.py": (135, 95)})

    divergence = compare_summaries(published, regenerated)

    assert divergence.kind == DIVERGENCE_COVERAGE
    assert divergence.coverage == 1
    text = "\n".join(diagnose(divergence, measured_files=1))
    assert "is stale: run 'make coverage-refresh'" in text
    assert "coverage change, not an interpreter mismatch" in text


def test_a_different_file_set_is_not_reported_as_a_measurement_change() -> None:
    """Summaries of different trees are not comparable, and say so."""

    from scripts.coverage_report import (
        DIVERGENCE_FILES,
        compare_summaries,
        diagnose,
    )

    published = _summary({"src/finance_sync/a.py": (10, 5)})
    regenerated = _summary({"src/finance_sync/b.py": (10, 5)})

    divergence = compare_summaries(published, regenerated)

    assert divergence.kind == DIVERGENCE_FILES
    assert divergence.files == 2
    assert "did not measure the same tree" in "\n".join(diagnose(divergence))


def test_an_identical_measurement_needs_no_action() -> None:
    from scripts.coverage_report import compare_summaries, diagnose

    summary = _summary({"src/finance_sync/a.py": (10, 5)})

    divergence = compare_summaries(summary, summary)

    assert divergence.identical
    assert "matches this run" in diagnose(divergence)[0]
