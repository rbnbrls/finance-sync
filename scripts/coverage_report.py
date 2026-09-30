#!/usr/bin/env python3
"""Publish this repository's coverage summary where a reader can parse it.

`make test-ci` -- the unit gate CI runs -- writes `coverage.xml` through
pytest-cov: a per-line Cobertura report, 1.5MB in this repository, uploaded as
the workflow artifact `coverage-report-3.12`. That artifact expires, and the
darkfactory quality lane does not read artifacts, so the measured number has to
live in the repository as well.

It cannot live there as the per-line report. The lane reads a committed file
through the provider's contents API, which answers with an *empty* body for any
file above 1MB, and truncates the text it does receive at 400,000 characters.
The merge that first committed `coverage.xml` (PR #1004, commit 8590e07f) made
the file readable in a browser and invisible to the reader: the lane's own
measurement of `main` still reported rung `enforced` with
`capability:coverage_not_published`, and `client.file_text` returned zero bytes
for a 1,536,189-byte report. A number nobody can read is not published, and a
report larger than the reader's fetch limit is worse than absent because it
*looks* published.

So this script publishes the summary instead: `coverage-summary.json` at the
repository root -- one entry per measured file plus the totals, around 56KB, in
the shape the lane parses (`total.lines.pct`). The per-line report keeps being
generated and uploaded by the workflow; only the reader-sized summary is
committed. Nothing is fabricated: every counter is derived from the Cobertura
report of the same run, and the per-file counters must sum to the totals the
reporter itself wrote, or the publication fails.

The summary is a pure function of what the tests executed -- no timestamp, no
absolute checkout path, files sorted, keys ordered -- so two runs of the same
measurement produce the same bytes. That is what makes the CI check in
`.github/workflows/ci.yml` meaningful: the `test` job measures, publishes, and
then requires `git diff --exit-code -- coverage-summary.json` to be clean. A
change that moves coverage must republish in the same pull request; an
unchanged measurement produces no diff at all. The workflow never commits the
summary itself: the job token keeps `contents: read`, so the publication
travels through the ordinary pull request and merge.

Three invariants are load-bearing, and `tests/test_coverage_report.py` asserts
them:

* the committed summary parses to a line percentage -- `total.lines.pct` is the
  field that moves the lane's coverage rung off `enforced`;
* it stays inside the reader's limits, which is why it is a summary: the
  committed file must be small enough for the provider to serve and for the
  lane to hold whole;
* publishing is idempotent, so an unchanged measurement leaves no diff.

Usage:
    python3 scripts/coverage_report.py              # publish the summary
    python3 scripts/coverage_report.py --summary    # ... and print the totals
    python3 scripts/coverage_report.py --check      # parse, write nothing
    python3 scripts/coverage_report.py --output other.json
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

#: What `pytest --cov --cov-report=xml` writes, per `[tool.coverage.xml]`.
DEFAULT_INPUT = "coverage.xml"
#: The committed summary: the path a reader (and the quality lane) looks for.
DEFAULT_OUTPUT = "coverage-summary.json"

#: `condition-coverage="50% (1/2)"` on a branch line: covered/total.
_CONDITION_RE = re.compile(r"\((\d+)/(\d+)\)")


@dataclass(frozen=True)
class Counter:
    """One measured surface (lines or branches) of a file, or of the run."""

    total: int
    covered: int

    @property
    def pct(self) -> float:
        return round(100.0 * self.covered / self.total, 2) if self.total else 0.0

    def as_json(self) -> dict[str, int | float]:
        return {
            "total": self.total,
            "covered": self.covered,
            "skipped": 0,
            "pct": self.pct,
        }


@dataclass(frozen=True)
class CoverageSummary:
    """The numbers a committed coverage summary is read for."""

    lines: Counter
    branches: Counter
    files: dict[str, dict[str, Counter]]

    @property
    def line_percent(self) -> float:
        return self.lines.pct

    def summary_line(self) -> str:
        text = (
            f"coverage: {self.lines.pct:.2f}% lines "
            f"({self.lines.covered}/{self.lines.total})"
        )
        # A repository that measures no branch (no condition coverage on any
        # line) would otherwise read as measured-and-empty.
        if self.branches.total:
            text += f", branches {self.branches.pct:.2f}%"
        return text

    def as_json(self) -> dict[str, object]:
        return {
            "total": {
                "lines": self.lines.as_json(),
                "branches": self.branches.as_json(),
            },
            "files": {
                name: {
                    "lines": counters["lines"].as_json(),
                    "branches": counters["branches"].as_json(),
                }
                for name, counters in sorted(self.files.items())
            },
        }


@dataclass(frozen=True)
class PublishedSummary:
    """What reading the committed summary back proves."""

    line_percent: float
    lines_covered: int
    lines_valid: int
    files: tuple[str, ...]


def _unreadable(path: Path, detail: str) -> ValueError:
    return ValueError(f"{path}: {detail}")


def read_report(path: str | Path) -> CoverageSummary:
    """Read a Cobertura report into the numbers the summary carries.

    Raises ``ValueError`` when the report cannot be read, and when its
    per-file detail does not add up to the totals the reporter itself wrote:
    a summary that contradicts its own source is not worth publishing.
    """

    report_path = Path(path)
    try:
        root = ElementTree.parse(report_path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise _unreadable(
            report_path, f"not readable as a coverage report ({exc})"
        ) from exc

    if root.tag != "coverage":
        raise _unreadable(report_path, f"root is <{root.tag}>, expected <coverage>")

    for name in ("line-rate", "lines-valid", "lines-covered"):
        if root.attrib.get(name) is None:
            raise _unreadable(report_path, f"no {name} attribute")

    def _count(name: str) -> int:
        try:
            return int(root.attrib.get(name, "0"))
        except ValueError as exc:
            raise _unreadable(report_path, f"{name} is not a number") from exc

    files: dict[str, dict[str, Counter]] = {}
    lines_seen = lines_covered_seen = 0
    branches_seen = branches_covered_seen = 0
    for element in root.iter("class"):
        name = element.attrib.get("filename") or element.attrib.get("name")
        if not name:
            raise _unreadable(report_path, "a <class> carries no filename")
        file_lines = list(element.iter("line"))
        covered = 0
        file_branches_covered = file_branches_total = 0
        for line in file_lines:
            if int(line.attrib.get("hits", "0") or 0) > 0:
                covered += 1
            condition = line.attrib.get("condition-coverage")
            if condition:
                match = _CONDITION_RE.search(condition)
                if match:
                    file_branches_covered += int(match.group(1))
                    file_branches_total += int(match.group(2))
        lines_seen += len(file_lines)
        lines_covered_seen += covered
        branches_seen += file_branches_total
        branches_covered_seen += file_branches_covered
        files[name] = {
            "lines": Counter(total=len(file_lines), covered=covered),
            "branches": Counter(
                total=file_branches_total, covered=file_branches_covered
            ),
        }

    if not files:
        raise _unreadable(report_path, "report carries no measured file")

    lines = Counter(total=_count("lines-valid"), covered=_count("lines-covered"))
    branches = Counter(
        total=_count("branches-valid"), covered=_count("branches-covered")
    )
    for label, seen, reported in (
        ("line", (lines_covered_seen, lines_seen), (lines.covered, lines.total)),
        (
            "branch",
            (branches_covered_seen, branches_seen),
            (branches.covered, branches.total),
        ),
    ):
        if seen != reported:
            raise _unreadable(
                report_path,
                f"per-file {label} counters {seen[0]}/{seen[1]} do not sum to "
                f"the reported totals {reported[0]}/{reported[1]}",
            )
    return CoverageSummary(lines=lines, branches=branches, files=files)


def write_summary(source: str | Path, destination: str | Path) -> CoverageSummary:
    """Write the published summary for the report at ``source``."""

    summary = read_report(source)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(summary.as_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_summary(path: str | Path) -> PublishedSummary:
    """Read a published summary back, raising ``ValueError`` when unreadable.

    This parses what a *reader* parses: `total.lines.pct` is the field the
    darkfactory quality lane reads, and the reason the file exists at all.
    """

    summary_path = Path(path)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _unreadable(
            summary_path, f"not readable as a coverage summary ({exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise _unreadable(summary_path, "top level is not an object")
    total = payload.get("total")
    lines = total.get("lines") if isinstance(total, dict) else None
    if not isinstance(lines, dict):
        raise _unreadable(summary_path, "no total.lines object")
    pct = lines.get("pct")
    if not isinstance(pct, (int, float)):
        raise _unreadable(summary_path, "total.lines.pct is not a number")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise _unreadable(summary_path, "no per-file entries")
    return PublishedSummary(
        line_percent=round(float(pct), 2),
        lines_covered=int(lines.get("covered", 0)),
        lines_valid=int(lines.get("total", 0)),
        files=tuple(sorted(files)),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    emit_summary = "--summary" in args

    output = DEFAULT_OUTPUT
    if "--output" in args:
        index = args.index("--output")
        try:
            output = args[index + 1]
        except IndexError:
            print("error: --output needs a path", file=sys.stderr)
            return 2
        del args[index : index + 2]

    paths = [arg for arg in args if not arg.startswith("-")]
    source = paths[0] if paths else (output if check_only else DEFAULT_INPUT)

    try:
        if check_only:
            published = parse_summary(source)
            if not emit_summary:
                return 0
            print(
                f"coverage: {published.line_percent:.2f}% lines "
                f"({published.lines_covered}/{published.lines_valid}), "
                f"{len(published.files)} files"
            )
            return 0
        summary = write_summary(source, output)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if emit_summary:
        print(summary.summary_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
