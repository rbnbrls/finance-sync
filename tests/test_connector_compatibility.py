"""Tests for the shared connector compatibility contract."""

from datetime import date

from finance_sync.services.connector_compatibility import evaluate_connector


def _lifecycle(**overrides):
    entry = {
        "name": "demo",
        "version": "1.2.3",
        "previous_version": "1.2.2",
        "capabilities": ["accounts", "transactions"],
        "minimum_fixture_version": "2026-01-15",
        "certification_status": "certified",
        "certified_at": "2026-08-22",
        "certification_commit": "test-commit",
        "deprecation_date": None,
        "removal_date": "2027-01-01",
    }
    entry.update(overrides)
    return {"connectors": [entry]}


def _metadata(**overrides):
    result = {
        "name": "demo",
        "provider_key": "demo",
        "plugin_version": "1.2.3",
        "supported_resources": ["accounts", "transactions"],
    }
    result.update(overrides)
    return result


def test_compatible_result_contains_safe_release_metadata() -> None:
    result = evaluate_connector(
        _lifecycle(),
        _metadata(),
        today=date(2026, 8, 26),
        fixture_version="2026-01-15",
    )

    assert result.status == "compatible"
    assert result.reason == "compatible"
    assert result.previous_version == "1.2.2"
    assert result.certification_commit == "test-commit"
    assert result.migration_required is False


def test_missing_lifecycle_entry_is_unavailable_and_requires_migration() -> (
    None
):
    result = evaluate_connector(
        {"connectors": []},
        _metadata(),
        today=date(2026, 8, 26),
    )

    assert result.status == "unavailable"
    assert result.reason == "lifecycle_entry_missing"
    assert result.migration_required is True


def test_version_and_capability_mismatch_are_incompatible() -> None:
    version = evaluate_connector(
        _lifecycle(),
        _metadata(plugin_version="1.2"),
        fixture_version="2026-01-15",
    )
    capability = evaluate_connector(
        _lifecycle(),
        _metadata(supported_resources=["accounts"]),
        fixture_version="2026-01-15",
    )

    assert (version.status, version.reason) == (
        "incompatible",
        "invalid_plugin_version",
    )
    assert (capability.status, capability.reason) == (
        "incompatible",
        "capability_mismatch",
    )
    assert version.migration_required
    assert capability.migration_required


def test_missing_certification_and_approaching_deprecation_are_warnings() -> (
    None
):
    result = evaluate_connector(
        _lifecycle(
            certification_status="unknown",
            deprecation_date="2026-09-01",
        ),
        _metadata(),
        today=date(2026, 8, 26),
        fixture_version="2026-01-15",
    )

    assert result.status == "attention_required"
    assert result.reason == "certification_missing"
    assert result.warnings == [
        "certification_missing",
        "deprecation_approaching",
    ]


def test_disabled_and_old_fixture_statuses_are_explicit() -> None:
    disabled = evaluate_connector(
        _lifecycle(), _metadata(), enabled=False, today=date(2026, 8, 26)
    )
    old_fixture = evaluate_connector(
        _lifecycle(),
        _metadata(),
        fixture_version="2025-01-01",
        today=date(2026, 8, 26),
    )

    assert (disabled.status, disabled.reason) == (
        "disabled",
        "feature_flag_disabled",
    )
    assert (old_fixture.status, old_fixture.reason) == (
        "incompatible",
        "fixture_too_old",
    )
