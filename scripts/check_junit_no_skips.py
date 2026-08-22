#!/usr/bin/env python3
"""Fail a required CI gate when its JUnit report contains skipped tests."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def count_skips(path: Path) -> int:
    """Return the number of skipped test cases in a JUnit XML report."""
    root = ET.parse(path).getroot()
    return len(root.findall(".//skipped"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    if not args.report.is_file():
        sys.stderr.write(f"JUnit report is missing: {args.report}\n")
        return 2
    skipped = count_skips(args.report)
    if skipped:
        sys.stderr.write(
            f"Required CI gate contains {skipped} skipped test(s): "
            f"{args.report}\n",
        )
        return 1
    sys.stdout.write(f"JUnit skip check passed: {args.report}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
