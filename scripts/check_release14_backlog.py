"""Audit that Release 14 stories are complete and traceable."""

# ruff: noqa: T201

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
BACKLOG = ROOT / "backlog"
STORIES = (
    "release14-analytics-history-component.md",
    "release14-scheduled-card-persistence.md",
    "release14-openapi-contract-artifact.md",
    "release14-release-smoke-evidence.md",
)


def audit() -> list[str]:
    errors: list[str] = []
    for filename in STORIES:
        path = BACKLOG / filename
        text = path.read_text(encoding="utf-8")
        if not re.search(r"^status:\s*done\s*$", text, re.MULTILINE):
            errors.append(f"{filename}: status is not done")
        if "## Implementatie en verificatie" not in text:
            errors.append(f"{filename}: missing verification section")
        criteria = re.findall(r"^- \[([ x])\]", text, re.MULTILINE)
        if not criteria or any(mark != "x" for mark in criteria):
            errors.append(f"{filename}: incomplete acceptance criterion")
    return errors


def main() -> int:
    errors = audit()
    if errors:
        print("Release 14 backlog audit failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Release 14 backlog audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
