"""Test that pyproject.toml is valid TOML with no duplicate keys.

The CI pipeline failed because pyproject.toml has duplicate keys
in [tool.ruff.lint.per-file-ignores], which causes uv to fail
with "TOML parse error: duplicate key".
"""

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_toml_parses_without_duplicate_keys():
    """Parse pyproject.toml and confirm no duplicate-key errors.

    A TOML parser will raise a tomllib.TOMLDecodeError on duplicate keys.
    """
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    assert pyproject_path.exists(), f"{pyproject_path} not found"

    with open(pyproject_path, "rb") as f:
        config = tomllib.load(f)

    # If we get here, the TOML parsed successfully with no duplicates
    assert "tool" in config
    assert "ruff" in config["tool"]
    assert "lint" in config["tool"]["ruff"]
    assert "per-file-ignores" in config["tool"]["ruff"]["lint"]

    # Check that there are no duplicate file entries by verifying
    # each file appears only once in the per-file-ignores
    ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]
    assert len(ignores) == len(set(ignores.keys())), (
        f"Duplicate file key(s) found in [tool.ruff.lint.per-file-ignores]: "
        f"{[k for k in ignores if list(ignores.keys()).count(k) > 1]}"
    )
