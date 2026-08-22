"""Release 14 OpenAPI diff artifact and policy tests."""

# pyright: basic

import json
from pathlib import Path
from typing import Any

from scripts.check_openapi_diff import diff_specs, main

ROOT = Path(__file__).parents[1]


def test_ci_uploads_openapi_snapshots_and_machine_readable_diff() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "openapi-base.json" in workflow
    assert "openapi-head.json" in workflow
    assert "--report openapi-diff.json" in workflow
    assert "openapi-diff.json" in workflow


def test_openapi_diff_allows_additions_and_rejects_breaking_changes(
    tmp_path: Path,
) -> None:
    base: dict[str, Any] = {
        "openapi": "3.1.0",
        "paths": {"/items": {"get": {}}},
    }
    additive: dict[str, Any] = {
        "openapi": "3.1.0",
        "paths": {"/items": {"get": {}}, "/new": {"get": {}}},
    }
    breaking: dict[str, Any] = {"openapi": "3.1.0", "paths": {}}
    assert not [
        item
        for item in diff_specs(base, additive)
        if item.severity == "breaking"
    ]
    assert any(
        item.kind == "removed_path" for item in diff_specs(base, breaking)
    )

    base_path = tmp_path / "base.json"
    head_path = tmp_path / "head.json"
    report_path = tmp_path / "diff.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    head_path.write_text(json.dumps(breaking), encoding="utf-8")
    assert (
        main(
            [
                "--base",
                str(base_path),
                "--head",
                str(head_path),
                "--report",
                str(report_path),
            ]
        )
        == 1
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["breaking"][0]["kind"] == "removed_path"
    assert "additive changes are allowed" in report["policy"]
