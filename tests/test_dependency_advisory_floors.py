"""Advisory floors: a locked dependency below its fixed release is a red CI.

`Security`'s "Scan dependencies for vulnerabilities" step runs pip-audit over
`uv export --locked`, so the *lockfile* is the gate's input, not a declared
range. When an advisory is published against a version this repository happens
to resolve, every branch turns red with no commit in between: 2.13.0 pyjwt on
2026-09-30, then 2.7.0 urllib3 the same day, each cleared only by restating an
explicit floor in `[project].dependencies` -- the one place a *transitive*
version can be pinned -- and re-locking.

pip-audit can only see that with a live advisory database and only inside CI, so
this file guards the other half offline: every floor the repository has had to
declare stays declared, and the lock keeps resolving at or above it. A floor
that is silently deleted is the dangerous state -- the lock still resolves a
fixed version today, so nothing goes red until the next refresh walks it back.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Package -> (lowest fixed release, the advisories the floor serves).
#: Package names are normalised the way the lockfile writes them.
ADVISORY_FLOORS: dict[str, tuple[str, str]] = {
    "multidict": ("6.9.1", "CVE-2026-104874"),
    "urllib3": ("2.8.0", "CVE-2026-97687, CVE-2026-97688, CVE-2026-97689"),
    "pyjwt": ("2.14.0", "CVE-2026-102265 through CVE-2026-102274"),
}


def _release_tuple(version: str) -> tuple[int, ...]:
    """Comparable release tuple for a PEP 440 version, pre-release suffix dropped."""
    release = version.split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for part in release.split("."):
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _locked_versions() -> dict[str, str]:
    with (REPO_ROOT / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    return {
        str(package["name"]): str(package["version"])
        for package in lock.get("package", [])
        if package.get("name") and package.get("version")
    }


def _declared_floors() -> dict[str, str]:
    """The `>=` floor declared for each package in `[project].dependencies`."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    floors: dict[str, str] = {}
    for requirement in project.get("dependencies", []):
        text = str(requirement)
        if ">=" not in text:
            continue
        name, _, floor = text.partition(">=")
        floors[name.strip().lower()] = floor.strip()
    return floors


def test_every_advisory_floor_is_declared_as_a_project_dependency() -> None:
    declared = _declared_floors()
    for package, (floor, advisories) in ADVISORY_FLOORS.items():
        assert package in declared, (
            f"{package} carries an advisory floor ({advisories}); removing it from "
            "[project].dependencies lets the next `uv lock` resolve a vulnerable "
            "version that only CI's pip-audit step would notice."
        )
        assert _release_tuple(declared[package]) >= _release_tuple(floor), (
            f"{package} is declared at >={declared[package]}, below the fixed release "
            f"{floor} ({advisories})."
        )


def test_locked_versions_satisfy_every_advisory_floor() -> None:
    locked = _locked_versions()
    for package, (floor, advisories) in ADVISORY_FLOORS.items():
        assert package in locked, (
            f"{package} carries an advisory floor but is absent from uv.lock; the "
            "floor is only meaningful while the package is still resolved."
        )
        assert _release_tuple(locked[package]) >= _release_tuple(floor), (
            f"uv.lock resolves {package} {locked[package]}, below the fixed release "
            f"{floor} ({advisories}); the `Security` job fails on this lockfile."
        )


def test_no_fix_advisory_is_ignored_in_local_and_ci_security_gates() -> None:
    """The no-fix python-jose advisory must not make the gates diverge."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    advisory = "GHSA-3qf3-8w2g-rqmx"
    assert f"--ignore-vuln {advisory}" in makefile
    assert f"--ignore-vuln {advisory}" in workflow
