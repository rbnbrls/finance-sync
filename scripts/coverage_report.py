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
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

#: What `pytest --cov --cov-report=xml` writes, per `[tool.coverage.xml]`.
DEFAULT_INPUT = "coverage.xml"
#: The committed summary: the path a reader (and the quality lane) looks for.
DEFAULT_OUTPUT = "coverage-summary.json"

#: The interpreter this repository measures with, pinned in `.python-version` so
#: `uv run` resolves the same one the workflow installs. It is load-bearing, not
#: documentation: see `compare_summaries`.
PYTHON_VERSION_FILE = ".python-version"

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


# --- diagnosing a divergence instead of telling a reader to guess ----------- #
#: The published summary is only reproducible when both sides measure with the
#: same interpreter. coverage.py counts a statement from the parse of the
#: interpreter running it, so one source file carries two statement totals under
#: two interpreters -- in this repository `src/finance_sync/api/v1/auth.py` is 135
#: statements under 3.14 and 164 under 3.12, and the published total is 32990
#: versus 33178. A gate that answers every divergence with "run `make
#: coverage-refresh` and commit the regenerated summary" therefore tells a reader
#: to publish exactly the artifact CI rejects, on every push, forever -- which is
#: how one interpreter mismatch held pull requests red across ~96 passes. Naming
#: the divergence is what ends that loop.
DIVERGENCE_IDENTICAL = "identical"
DIVERGENCE_STATEMENTS = "statement_totals"
DIVERGENCE_COVERAGE = "coverage_only"
DIVERGENCE_FILES = "measured_files"

#: How many differing files a diagnosis names before it summarizes the rest.
_EXAMPLE_LIMIT = 5


@dataclass(frozen=True)
class Divergence:
    """How two summaries of the same tree differ, and therefore why."""

    kind: str
    statements: int = 0
    coverage: int = 0
    files: int = 0
    examples: tuple[str, ...] = ()

    @property
    def identical(self) -> bool:
        return self.kind == DIVERGENCE_IDENTICAL


def _read_payload(path: str | Path) -> dict[str, Any]:
    """Read a summary's raw JSON, raising ``ValueError`` when unreadable."""
    summary_path = Path(path)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _unreadable(
            summary_path, f"not readable as a coverage summary ({exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise _unreadable(summary_path, "top level is not an object")
    return payload


def _counts(entry: object, surface: str) -> tuple[int, int]:
    """``(total, covered)`` for one surface of one file entry, or ``(0, 0)``."""
    block = entry.get(surface) if isinstance(entry, Mapping) else None
    if not isinstance(block, Mapping):
        return 0, 0
    try:
        return int(block.get("total") or 0), int(block.get("covered") or 0)
    except (TypeError, ValueError):
        return 0, 0


def compare_summaries(
    published: Mapping[str, Any], regenerated: Mapping[str, Any]
) -> Divergence:
    """Classify the difference between a committed and a regenerated summary.

    Three classes, in the order that makes each one decidable:

    * ``measured_files`` -- the two summaries do not describe the same files, so
      they did not measure the same tree and there is nothing to compare;
    * ``statement_totals`` -- the same files disagree on how many statements they
      contain. A statement total is a property of the source file *as the
      measuring interpreter parsed it*, so this means the two runs used different
      interpreters (or different trees). Refreshing here cannot converge;
    * ``coverage_only`` -- identical statement totals, different hits. That is a
      real coverage movement, and the ordinary reason to refresh.
    """
    published_files = published.get("files")
    regenerated_files = regenerated.get("files")
    published_files = published_files if isinstance(published_files, Mapping) else {}
    regenerated_files = (
        regenerated_files if isinstance(regenerated_files, Mapping) else {}
    )

    differing_names = set(published_files) ^ set(regenerated_files)
    if differing_names:
        return Divergence(
            DIVERGENCE_FILES,
            files=len(differing_names),
            examples=tuple(sorted(differing_names)[:_EXAMPLE_LIMIT]),
        )

    statements: list[str] = []
    coverage: list[str] = []
    for name in sorted(published_files):
        before_total, before_covered = _counts(published_files[name], "lines")
        after_total, after_covered = _counts(regenerated_files[name], "lines")
        if before_total != after_total:
            statements.append(f"{name} ({before_total} -> {after_total})")
            continue
        if before_covered != after_covered or _counts(
            published_files[name], "branches"
        ) != _counts(regenerated_files[name], "branches"):
            coverage.append(name)

    if statements:
        return Divergence(
            DIVERGENCE_STATEMENTS,
            statements=len(statements),
            coverage=len(coverage),
            examples=tuple(statements[:_EXAMPLE_LIMIT]),
        )
    if coverage:
        return Divergence(
            DIVERGENCE_COVERAGE,
            coverage=len(coverage),
            examples=tuple(coverage[:_EXAMPLE_LIMIT]),
        )
    return Divergence(DIVERGENCE_IDENTICAL)


def running_interpreter() -> str:
    """The version of the interpreter doing the measuring."""
    return "{}.{}.{}".format(*sys.version_info[:3])


def pinned_interpreter(root: Path | None = None) -> str:
    """The version `.python-version` pins, or ``""`` when it is absent."""
    path = Path(root or Path(__file__).resolve().parents[1]) / PYTHON_VERSION_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def committed_summary(ref: str = "HEAD", *, root: Path | None = None) -> dict[str, Any]:
    """Read the committed summary through ``git``, never from the working tree."""
    location = Path(root or Path(__file__).resolve().parents[1])
    result = subprocess.run(
        ["git", "show", f"{ref}:{DEFAULT_OUTPUT}"],
        cwd=location,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot read {ref}:{DEFAULT_OUTPUT} from git")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ref}:{DEFAULT_OUTPUT} is not readable JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{ref}:{DEFAULT_OUTPUT} is not a JSON object")
    return payload


def diagnose(
    divergence: Divergence, *, measured_files: int = 0, root: Path | None = None
) -> list[str]:
    """What a reader needs to act on this divergence, and nothing else."""
    measured = f" of {measured_files}" if measured_files else ""
    examples = ", ".join(divergence.examples)
    pin = pinned_interpreter(root)
    if divergence.identical:
        return [
            f"{DEFAULT_OUTPUT} matches this run "
            f"(measured on Python {running_interpreter()})"
        ]
    if divergence.kind == DIVERGENCE_STATEMENTS:
        return [
            f"{DEFAULT_OUTPUT} diverges from this run in its statement totals: "
            f"{divergence.statements}{measured} measured files disagree on how many "
            f"statements they contain, for example {examples}.",
            "A statement total is a property of the source file as the measuring "
            "interpreter parsed it, so the committed summary and this run were not "
            f"measured by the same interpreter (this run used Python "
            f"{running_interpreter()}"
            + (f", the repository pins {pin})." if pin else ")."),
            "Refreshing here cannot converge: committing this run's summary would publish "
            "a file CI disagrees with in exactly the same files. Re-measure with the "
            f"interpreter CI uses ({pin or 'unpinned'}) and commit that summary.",
        ]
    if divergence.kind == DIVERGENCE_COVERAGE:
        return [
            f"{DEFAULT_OUTPUT} is stale: run 'make coverage-refresh' and commit the "
            f"regenerated summary.",
            f"{divergence.coverage}{measured} measured files carry identical statement "
            f"totals with different results, for example {examples}. The two measurements "
            f"agree on what is measurable, so this is a coverage change, not an "
            f"interpreter mismatch.",
        ]
    return [
        f"{DEFAULT_OUTPUT} describes a different set of files than this run: "
        f"{divergence.files} file(s) appear on only one side, for example {examples}.",
        "The two summaries did not measure the same tree, so there is nothing to "
        "compare. Re-check the checkout and re-measure.",
    ]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    emit_summary = "--summary" in args

    # `--compare [REF]` answers the question `coverage-check` actually asks: the
    # working tree holds a freshly published summary, so a plain `git diff`
    # cannot say *why* it differs from the committed one. The ref is read through
    # git, so the comparison never depends on the working-tree copy.
    compare_ref: str | None = None
    if "--compare" in args:
        index = args.index("--compare")
        following = args[index + 1] if index + 1 < len(args) else ""
        if following and not following.startswith("-"):
            compare_ref = following
            del args[index : index + 2]
        else:
            compare_ref = "HEAD"
            del args[index]
    for arg in list(args):
        if arg.startswith("--compare="):
            compare_ref = arg.split("=", 1)[1] or "HEAD"
            args.remove(arg)

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

    if compare_ref is not None:
        try:
            published = committed_summary(compare_ref)
            regenerated = _read_payload(output)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        divergence = compare_summaries(published, regenerated)
        measured_files = len(regenerated.get("files") or {})
        for line in diagnose(divergence, measured_files=measured_files):
            print(line, file=sys.stderr)
        return 0 if divergence.identical else 1

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
