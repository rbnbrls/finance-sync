#!/usr/bin/env python3
"""Publish this repository's coverage report so a reader can trust the number.

`pytest --cov --cov-report=xml` writes `coverage.xml` (see `[tool.coverage.xml]`
in `pyproject.toml`), and that file on its own is not evidence anyone can read
later: CI uploads it as a run artifact, which expires, and the darkfactory
quality lane does not read an artifact. The lane reads a *committed*,
machine-readable report from the default branch -- `coverage.xml` first -- and
reports the coverage rung as `enforced` with `capability:coverage_not_published`
while that file is absent, which is exactly the state this repository was
measured in on 2026-09-26.

This script is the one step between the two: it parses the Cobertura report and
writes the normalised copy to the published path (`coverage.xml` at the
repository root, which is the same path pytest-cov wrote). Two volatile fields
are removed:

* `timestamp` -- the wall-clock time of the run, which would otherwise make two
  identical measurements differ;
* the collected `<source>` roots -- `[tool.coverage.run] relative_files` already
  writes every `filename` relative to the working directory, and any absolute
  checkout prefix left in `<source>` (`/home/...`, `/home/runner/work/...`)
  would make the committed report machine-specific. The report is published at
  the repository root, so `.` is the base it actually describes.

Across those two fields the report is a pure function of what the tests
executed, which is what makes the CI check in `.github/workflows/ci.yml`
meaningful: the `test` job measures coverage on the runner, normalises the
report and requires `git diff --exit-code -- coverage.xml` to be clean. A change
that moves coverage must republish the report in the same pull request; an
unchanged measurement produces no diff at all. The report is never written back
by CI: a commit from the workflow would need a write grant on the workflow
token, so it travels through the ordinary pull request and merge instead.

Three invariants are load-bearing, and `tests/test_coverage_report.py` asserts
them:

* the report at the repository root parses to a line percentage -- the quality
  lane reads `coverage.xml` from the default branch and only moves the coverage
  rung on when `line-rate` parses to a number;
* the published report carries no absolute checkout path and no timestamp, so a
  run on any machine produces the same bytes;
* normalising an already-normalised report is a no-op.

Usage:
    python3 scripts/coverage_report.py              # normalise coverage.xml
    python3 scripts/coverage_report.py --summary    # ... and print the totals
    python3 scripts/coverage_report.py --check      # parse, write nothing
    python3 scripts/coverage_report.py --output other.xml
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

#: What `pytest --cov --cov-report=xml` writes, per `[tool.coverage.xml]`.
DEFAULT_INPUT = "coverage.xml"
#: Where a reader (and the quality lane) looks first: the repository root.
DEFAULT_OUTPUT = "coverage.xml"


@dataclass(frozen=True)
class CoverageSummary:
    """The numbers a committed coverage report is read for."""

    line_rate: float
    lines_covered: int
    lines_valid: int
    branch_rate: float | None
    branches_valid: int = 0

    @property
    def line_percent(self) -> float:
        return round(self.line_rate * 100.0, 2)

    def summary_line(self) -> str:
        text = (
            f"coverage: {self.line_percent:.2f}% lines "
            f"({self.lines_covered}/{self.lines_valid})"
        )
        # A repository that does not measure branches (branches-valid="0")
        # would otherwise read as measured-and-empty rather than not-measured.
        if self.branch_rate is not None and self.branches_valid:
            text += f", branches {round(self.branch_rate * 100.0, 2):.2f}%"
        return text


def parse_report(path: str | Path) -> CoverageSummary:
    """Parse a Cobertura report, raising ``ValueError`` when unreadable.

    A report that cannot be parsed is an error and never a percentage: the
    whole point of publishing the file is that a reader can trust the number
    in it.
    """

    report_path = Path(path)
    try:
        root = ElementTree.parse(report_path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        message = f"{report_path}: not readable as a coverage report ({exc})"
        raise ValueError(message) from exc

    if root.tag != "coverage":
        message = f"{report_path}: root is <{root.tag}>, expected <coverage>"
        raise ValueError(message)

    raw_rate = root.attrib.get("line-rate") or root.attrib.get("line_rate")
    if raw_rate is None:
        raise ValueError(f"{report_path}: no line-rate attribute")
    try:
        line_rate = float(raw_rate)
    except ValueError as exc:
        message = f"{report_path}: line-rate {raw_rate!r} is not a number"
        raise ValueError(message) from exc

    def _int(name: str) -> int:
        try:
            return int(root.attrib.get(name, "0"))
        except ValueError:
            return 0

    branch_raw = root.attrib.get("branch-rate") or root.attrib.get(
        "branch_rate"
    )
    try:
        branch_rate = float(branch_raw) if branch_raw is not None else None
    except ValueError:
        branch_rate = None

    return CoverageSummary(
        line_rate=line_rate,
        lines_covered=_int("lines-covered"),
        lines_valid=_int("lines-valid"),
        branch_rate=branch_rate,
        branches_valid=_int("branches-valid"),
    )


def _normalise_sources(root: ElementTree.Element) -> None:
    """Repoint the report's source roots at the directory it is published in.

    coverage.py writes one `<source>` per collected root; with
    `relative_files` enabled the element is empty, and a report generated in a
    checkout still names the absolute path (`/home/runner/work/<repo>/<repo>`).
    Either way the report describes the repository root, so it publishes `.`
    exactly once and never a path from the machine that measured it.
    """

    sources = root.find("./sources")
    if sources is None:
        sources = ElementTree.SubElement(root, "sources")
        sources.tail = "\n\t"
        # Keep <sources> ahead of <packages> the way the reporter orders it.
        root.remove(sources)
        root.insert(0, sources)
    for element in list(sources):
        sources.remove(element)
    source = ElementTree.SubElement(sources, "source")
    source.text = "."
    sources.text = "\n\t\t"
    source.tail = "\n\t"


def normalise_report(
    path: str | Path, destination: str | Path | None = None
) -> CoverageSummary:
    """Write the published, machine-independent copy of a coverage report.

    ``destination`` defaults to the input path, so normalising in place is the
    documented behaviour; an explicit ``--output`` publishes somewhere else.
    """

    source = Path(path)
    target = Path(destination) if destination is not None else source
    summary = parse_report(source)

    # Comments carry the coverage.py version that produced the report; keep
    # them rather than letting the parser drop the provenance.
    parser = ElementTree.XMLParser(
        target=ElementTree.TreeBuilder(insert_comments=True)
    )
    tree = ElementTree.parse(source, parser=parser)
    root = tree.getroot()
    root.attrib.pop("timestamp", None)
    _normalise_sources(root)
    if target != source:
        target.parent.mkdir(parents=True, exist_ok=True)
    # ElementTree keeps attribute order, so a re-run on the output of this
    # function rewrites identical bytes.
    tree.write(target, encoding="utf-8", xml_declaration=True)

    body = target.read_bytes()
    if not body.endswith(b"\n"):
        # ElementTree writes no trailing newline; a committed text file should
        # end with one.
        target.write_bytes(body + b"\n")
    return summary


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
    report_path = paths[0] if paths else DEFAULT_INPUT

    try:
        summary = (
            parse_report(report_path)
            if check_only
            else normalise_report(report_path, output)
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if emit_summary:
        print(summary.summary_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
