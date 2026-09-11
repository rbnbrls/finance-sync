"""Shared connector compatibility and lifecycle evaluation.

This module is deliberately pure: API handlers, workers and operational
scripts can use the same status rules without opening a database or touching
credentials.  Only synthetic lifecycle/contract metadata is accepted.
"""

# pyright: basic

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from finance_sync.schemas.connector_compatibility import ConnectorCompatibility

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _date_is_invalid(value: Any) -> bool:
    return value not in (None, "") and _parse_date(value) is None


def _metadata_version(metadata: dict[str, Any]) -> str | None:
    # The public ``/connectors`` catalogue exposes the SDK version, while the
    # internal registry also has the implementation version.  Treat the SDK
    # version as a safe fallback so a connector is not shown as unknown merely
    # because it did not publish a separate plugin version.
    value = metadata.get("plugin_version", metadata.get("sdk_version"))
    return str(value) if value is not None else None


def _certification_from_matrix(
    provider_key: str,
    version: str | None,
    matrix: dict[str, Any] | None,
) -> dict[str, Any]:
    if not matrix:
        return {}
    for item in matrix.get("connectors", []):
        if (
            isinstance(item, dict)
            and item.get("name") == provider_key
            and item.get("version") == version
        ):
            return {
                "certification_status": "certified",
                "certified_at": item.get("fixture_date"),
                "certification_commit": "synthetic-contract-matrix",
            }
    return {}


def evaluate_connector(
    lifecycle: dict[str, Any],
    metadata: dict[str, Any],
    *,
    today: date | None = None,
    fixture_version: str | None = None,
    enabled: bool = True,
    contract_matrix: dict[str, Any] | None = None,
) -> ConnectorCompatibility:
    """Evaluate one installed connector against lifecycle contracts."""
    today = today or datetime.now(UTC).date()
    provider_key = str(metadata.get("provider_key", metadata.get("name", "")))
    version = _metadata_version(metadata)
    entry = next(
        (
            item
            for item in lifecycle.get("connectors", [])
            if isinstance(item, dict) and item.get("name") == provider_key
        ),
        None,
    )
    if not isinstance(entry, dict):
        return ConnectorCompatibility(
            provider_key=provider_key,
            status="unavailable",
            reason="lifecycle_entry_missing",
            current_version=version,
            migration_required=True,
        )

    previous_version = entry.get("previous_version")
    minimum_fixture = entry.get("minimum_fixture_version")
    deprecation_date = _parse_date(entry.get("deprecation_date"))
    removal_date = _parse_date(entry.get("removal_date"))
    certification = {
        **_certification_from_matrix(provider_key, version, contract_matrix),
        **{
            key: entry[key]
            for key in (
                "certification_status",
                "certified_at",
                "certification_commit",
            )
            if key in entry
        },
    }
    certified_at = _parse_date(certification.get("certified_at"))
    warnings: list[str] = []
    migration_required = bool(entry.get("migration_required", False))

    result = ConnectorCompatibility(
        provider_key=provider_key,
        status="compatible",
        reason="compatible",
        current_version=version,
        previous_version=(str(previous_version) if previous_version else None),
        minimum_fixture_version=(
            str(minimum_fixture) if minimum_fixture else None
        ),
        certification_status=str(
            certification.get("certification_status", "unknown")
        ),
        certified_at=certified_at,
        certification_commit=(
            str(certification["certification_commit"])
            if certification.get("certification_commit")
            else None
        ),
        deprecation_date=deprecation_date,
        removal_date=removal_date,
        migration_required=migration_required,
        warnings=warnings,
    )

    if not enabled:
        result.status, result.reason = "disabled", "feature_flag_disabled"
        return result

    lifecycle_version = entry.get("version")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        result.status, result.reason = "incompatible", "invalid_plugin_version"
        result.migration_required = True
        return result
    if not isinstance(lifecycle_version, str) or not _SEMVER.fullmatch(
        lifecycle_version
    ):
        result.status, result.reason = (
            "incompatible",
            "invalid_lifecycle_version",
        )
        result.migration_required = True
        return result
    if version != lifecycle_version:
        result.status, result.reason = "incompatible", "version_mismatch"
        result.migration_required = True
        return result
    if _date_is_invalid(entry.get("deprecation_date")) or _date_is_invalid(
        entry.get("removal_date")
    ):
        result.status, result.reason = (
            "incompatible",
            "invalid_lifecycle_date",
        )
        result.migration_required = True
        return result
    if (
        fixture_version
        and minimum_fixture
        and fixture_version < str(minimum_fixture)
    ):
        result.status, result.reason = "incompatible", "fixture_too_old"
        result.migration_required = True
        return result

    expected_capabilities = set(entry.get("capabilities", []))
    actual_capabilities = set(metadata.get("supported_resources", []))
    if expected_capabilities != actual_capabilities:
        result.status, result.reason = "incompatible", "capability_mismatch"
        result.migration_required = True
        return result

    certification_status = result.certification_status.lower()
    if certification_status not in {"certified", "valid"}:
        warnings.append("certification_missing")
        result.status, result.reason = (
            "attention_required",
            "certification_missing",
        )

    if deprecation_date:
        if removal_date and today >= removal_date:
            warnings.append("removal_date_reached")
            result.status, result.reason = "deprecated", "removal_date_reached"
        elif today >= deprecation_date:
            warnings.append("deprecation_date_reached")
            result.status, result.reason = (
                "deprecated",
                "deprecation_date_reached",
            )
        elif today + timedelta(days=30) >= deprecation_date:
            warnings.append("deprecation_approaching")
            if result.status == "compatible":
                result.status, result.reason = (
                    "attention_required",
                    "deprecation_approaching",
                )
    if migration_required and result.status == "compatible":
        result.status, result.reason = (
            "attention_required",
            "migration_required",
        )
    result.warnings = warnings
    return result


def evaluate_connectors(
    lifecycle: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    *,
    today: date | None = None,
    fixture_version: str | None = None,
    enabled: bool = True,
    contract_matrix: dict[str, Any] | None = None,
) -> list[ConnectorCompatibility]:
    """Evaluate all installed catalog entries in stable key order."""
    return [
        evaluate_connector(
            lifecycle,
            catalog[name],
            today=today,
            fixture_version=fixture_version,
            enabled=enabled,
            contract_matrix=contract_matrix,
        )
        for name in sorted(catalog)
    ]


def load_json(path: Path) -> dict[str, Any]:
    """Load a synthetic JSON contract, returning an empty object if absent."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def default_contract_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[3]
    return (
        root / "config" / "connector-lifecycle.json",
        root / "config" / "provider-contract-matrix.json",
    )
