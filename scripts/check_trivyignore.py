"""Validate the bounded accepted-risk policy used by Trivy."""

from __future__ import annotations

import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path


def parse_entries(path: Path) -> list[tuple[str, str, str]]:
    """Return ``(finding, expiry, rationale)`` records from a baseline."""
    entries: list[tuple[str, str, str]] = []
    comments: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            comments.append(line[1:].strip())
            continue
        if not line:
            continue
        expiry = ""
        for comment in comments:
            match = re.search(r'expiry="([^"]+)"$', comment)
            if match:
                expiry = match.group(1)
        entries.append((line, expiry, " ".join(comments)))
        comments = []
    return entries


def validate(path: Path, *, today: date | None = None) -> None:
    """Raise ``ValueError`` for malformed, duplicate or expired entries."""
    entries = parse_entries(path)
    seen: set[str] = set()
    current = today or datetime.now(UTC).date()
    for finding, expiry, rationale in entries:
        if finding in seen:
            message = f"duplicate accepted-risk entry: {finding}"
            raise ValueError(message)
        seen.add(finding)
        if not expiry:
            message = f"{finding} has no expiry= marker"
            raise ValueError(message)
        try:
            expiry_date = date.fromisoformat(expiry)
        except ValueError as exc:
            message = f"{finding} has invalid expiry={expiry}"
            raise ValueError(message) from exc
        if expiry_date < current:
            message = f"{finding} expired on {expiry}"
            raise ValueError(message)
        if not rationale:
            message = f"{finding} has no rationale"
            raise ValueError(message)


def main() -> int:
    """Validate the path supplied on the command line."""
    if len(sys.argv) != 2:
        sys.stderr.write("usage: check_trivyignore.py PATH\n")
        return 2
    try:
        validate(Path(sys.argv[1]))
    except ValueError as exc:
        sys.stderr.write(f"trivyignore policy failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
