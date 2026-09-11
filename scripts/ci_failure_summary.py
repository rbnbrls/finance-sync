"""Print a compact, actionable summary for a failed CI test job."""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _first_line(value: str | None) -> str:
    for line in (value or "").splitlines():
        line = line.strip()
        if line:
            return line[:500]
    return "no failure message recorded"


def _junit_summary(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return [f"JUnit parse failure: {exc}"]

    failures: list[str] = []
    for case in root.iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is not None:
            name = ".".join(
                value
                for value in (case.get("classname"), case.get("name"))
                if value
            )
            failures.append(f"{name}: {_first_line(failure.text)}")
    return failures[:5]


def _log_summary(path: Path) -> list[str]:
    if not path.exists():
        return []
    patterns = re.compile(
        r"(?:ImportError|ModuleNotFoundError|AssertionError|FAILED|ERROR|E\s|Error:)"
    )
    lines = [
        line.strip() for line in path.read_text(errors="replace").splitlines()
    ]
    return [line[:500] for line in lines if patterns.search(line)][:5]


def build_summary(junit: Path, log: Path | None, title: str) -> str:
    findings = _junit_summary(junit)
    if not findings and log is not None:
        findings = _log_summary(log)
    if not findings:
        findings = [
            "No structured failure detail was available; inspect the job log."
        ]
    lines = [
        f"## {title}",
        "",
        f"Commit: `{os.environ.get('GITHUB_SHA', 'local')}`",
        "",
    ]
    lines.extend(f"- {finding}" for finding in findings)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--title", default="CI failure summary")
    args = parser.parse_args()
    summary = build_summary(args.junit, args.log, args.title)
    sys.stdout.write(summary)
    output = os.environ.get("GITHUB_STEP_SUMMARY")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
