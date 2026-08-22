"""Reject credentials and sensitive application values in scan artifacts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, cast

_SENSITIVE_KEY = re.compile(
    r"(password|secret|token|credential|api[_-]?key)", re.IGNORECASE
)
_CREDENTIAL_URL = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE
)


def _walk(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        for key, child in mapping.items():
            child_path = f"{path}.{key}"
            if _SENSITIVE_KEY.search(str(key)):
                findings.append(child_path)
            findings.extend(_walk(child, child_path))
    elif isinstance(value, list):
        sequence = cast("list[Any]", value)
        for index, child in enumerate(sequence):
            findings.extend(_walk(child, f"{path}[{index}]"))
    elif isinstance(value, str) and _CREDENTIAL_URL.search(value):
        findings.append(path)
    return findings


def validate(paths: list[Path]) -> None:
    """Validate that all scan outputs are JSON and contain no credentials."""
    findings: list[str] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        findings.extend(f"{path}:{item}" for item in _walk(payload))
    if findings:
        raise ValueError(
            "sensitive values in security evidence: " + ", ".join(findings)
        )


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: check_security_evidence.py ARTIFACT...\n")
        return 2
    try:
        validate([Path(item) for item in sys.argv[1:]])
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"security evidence policy failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
