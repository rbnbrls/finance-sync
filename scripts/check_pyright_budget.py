#!/usr/bin/env python3
# pyright: basic
"""Fail when the source Pyright warning count exceeds its baseline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("paths", nargs="+", default=["src"])
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    result = subprocess.run(
        ["pyright", "--outputjson", *args.paths],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(result.stderr or result.stdout)
        return result.returncode or 1

    summary = report["summary"]
    warnings = int(summary["warningCount"])
    errors = int(summary["errorCount"])
    maximum = int(baseline["max_warnings"])
    sys.stdout.write(
        f"Pyright: {errors} errors, {warnings} warnings (budget {maximum})\n"
    )
    if errors or warnings > maximum:
        if warnings > maximum:
            sys.stderr.write(
                f"Pyright warning budget exceeded: {warnings} > {maximum}",
            )
            sys.stderr.write("\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
