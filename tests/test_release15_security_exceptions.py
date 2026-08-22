"""Release 15 security exception lifecycle contracts."""

# pyright: basic

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.security_exception_report import build_report

ROOT = Path(__file__).parents[1]


def test_exception_report_has_owner_issue_expiry_and_no_sensitive_data() -> (
    None
):
    report = build_report(ROOT / ".trivyignore", today=date(2026, 8, 22))
    entries: list[dict[str, Any]] = report["entries"]  # type: ignore[assignment]
    assert entries
    assert all(entry["advisory"].startswith("CVE-") for entry in entries)
    assert all(entry["owner"] for entry in entries)
    assert all(entry["issue"].startswith("https://") for entry in entries)
    assert all(entry["expiry"] for entry in entries)
    assert report["contains_secrets"] is False
    assert report["contains_financial_data"] is False
    json.dumps(report)


def test_expired_exception_fails_and_removed_exception_disappears(
    tmp_path: Path,
) -> None:
    source = tmp_path / ".trivyignore"
    source.write_text('# rationale\n# expiry="2020-01-01"\nCVE-TEST\n')
    with pytest.raises(ValueError, match="expired"):
        build_report(source, today=date(2026, 8, 22))

    source.write_text('# rationale\n# expiry="2099-01-01"\nCVE-TEST\n')
    report = build_report(source, today=date(2026, 8, 22))
    entries: list[dict[str, Any]] = report["entries"]  # type: ignore[assignment]
    assert [entry["advisory"] for entry in entries] == ["CVE-TEST"]


def test_security_ci_publishes_exception_report() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts.security_exception_report" in workflow
    assert "security-exceptions.json" in workflow
