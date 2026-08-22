"""Release 17 provider contract refresh tests."""

# pyright: basic

import json
from pathlib import Path

import pytest

from scripts.provider_contract_refresh import validate_matrix


def _matrix() -> dict:
    return json.loads(Path("config/provider-contract-matrix.json").read_text())


def test_matrix_covers_supported_connectors_without_real_data() -> None:
    matrix = _matrix()
    validate_matrix(matrix)
    assert {item["name"] for item in matrix["connectors"]} >= {
        "bunq",
        "trading212",
        "degiro_pension",
        "ynab",
    }
    assert matrix["synthetic_data_only"] is True
    assert "api_key" not in json.dumps(matrix).lower()
    assert "personal data" not in json.dumps(matrix).lower()


def test_missing_field_and_invalid_enum_are_clear_failures() -> None:
    matrix = _matrix()
    del matrix["connectors"][0]["fixtures"]["accounts"]["fields"]["status"]
    with pytest.raises(ValueError, match="missing fields"):
        validate_matrix(matrix)
    matrix = _matrix()
    matrix["connectors"][0]["fixtures"]["accounts"]["fields"]["status"] = (
        "enum:"
    )
    with pytest.raises(ValueError, match="empty enum"):
        validate_matrix(matrix)


def test_ci_validates_and_publishes_contract_report() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "provider-contracts:" in workflow
    assert "provider_contract_refresh.py" in workflow
    assert "provider-contract-report.json" in workflow
