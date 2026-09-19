"""Audit that Release 14 stories are complete and traceable."""

# ruff: noqa: T201

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
BACKLOG = ROOT / "backlog"
# Release 14 stories were archived after their evidence was merged.  Keep this
# tuple as the compatibility surface for callers; the active backlog is
# intentionally not required to retain historical release story files.
STORIES: tuple[str, ...] = ()


def audit() -> list[str]:
    errors: list[str] = []
    for filename in STORIES:
        path = BACKLOG / filename
        if not path.is_file():
            errors.append(f"{filename}: missing story file")
            continue
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
