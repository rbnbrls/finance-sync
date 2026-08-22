"""Release 18 connector lifecycle contracts."""

# pyright: basic

import json
from datetime import date
from pathlib import Path

from scripts.connector_lifecycle import evaluate


def _lifecycle() -> dict:
    return json.loads(Path("config/connector-lifecycle.json").read_text())


def test_registry_lifecycle_reports_capabilities_and_rollback_safely() -> None:
    diagnostics = evaluate(_lifecycle(), today=date(2026, 8, 22), fixture_version="2026-01-15")
    assert {item["connector"] for item in diagnostics} == {"bunq", "trading212", "degiro_pension", "ynab"}
    assert all(item["status"] == "healthy" for item in diagnostics)
    assert all(item["rollback_version"] == "0.0.9" for item in diagnostics)
    assert all(item["credentials_included"] is False for item in diagnostics)


def test_deprecation_feature_flag_and_fixture_failures_are_explicit() -> None:
    lifecycle = _lifecycle()
    lifecycle["connectors"][0]["deprecation_date"] = "2026-01-01"
    deprecated = evaluate(lifecycle, today=date(2026, 8, 22), fixture_version="2026-01-15")
    assert deprecated[0]["status"] == "deprecated"
    disabled = evaluate(_lifecycle(), today=date(2026, 8, 22), fixture_version="2026-01-15", enabled=False)
    assert all(item["status"] == "disabled" for item in disabled)
    old_fixture = evaluate(_lifecycle(), today=date(2026, 8, 22), fixture_version="2025-01-01")
    assert all(item["status"] == "incompatible" for item in old_fixture)


def test_ci_validates_connector_lifecycle_report() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "connector-lifecycle:" in workflow
    assert "connector_lifecycle.py" in workflow
    assert "connector-lifecycle-report.json" in workflow
