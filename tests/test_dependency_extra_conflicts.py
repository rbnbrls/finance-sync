"""Extras that cannot be installed together must be declared conflicting.

uv resolves the *cross product* of every declared optional-dependency extra by
default, so one unsatisfiable pair makes the whole lock un-upgradable even
while `uv lock --check` stays green: the committed resolutions are fine, but
every `uv lock --upgrade-package` for a package the pair disagrees about exits
1. That is exactly what made the weekly "Dependabot Updates" workflow red
(incident 4b896e55, run #11): the ``openbb`` extra's ``openbb-core`` pins
``fastapi`` and ``ruff`` to the releases it accepts while ``dev`` holds them
above, so Dependabot's per-package upgrade of fastapi/ruff/openbb each failed
with ``dependency_file_not_resolvable`` and the job exited 1.

The repair is ``[tool.uv].conflicts``: uv then resolves each side in its own
fork, and the fork split is written into ``uv.lock`` as resolution markers that
select one extra and exclude the other. This module guards both halves offline
-- the declaration stays present, and the lock still proves the forks -- so
deleting the declaration (or re-locking without it) fails here before the next
Dependabot run has to.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: uv names the fork for an extra ``<name>`` as ``extra == 'extra-<n>-<dist>-<name>'``.
#: The ordinal is uv's own, so match it loosely rather than pinning a revision.
_EXTRA_MARKER = re.compile(
    r"extra\s*==\s*'extra-[0-9]+-finance-sync-([A-Za-z0-9_.-]+)'"
)
_EXCLUDED_MARKER = re.compile(
    r"extra\s*!=\s*'extra-[0-9]+-finance-sync-([A-Za-z0-9_.-]+)'"
)


def _pyproject() -> dict[str, Any]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _lock() -> dict[str, Any]:
    with (REPO_ROOT / "uv.lock").open("rb") as handle:
        return tomllib.load(handle)


def _declared_extras() -> set[str]:
    return set(_pyproject().get("project", {}).get("optional-dependencies", {}))


def _declared_conflict_pairs() -> list[set[str]]:
    """Every extra-vs-extra conflict declared in ``[tool.uv].conflicts``."""
    conflicts = _pyproject().get("tool", {}).get("uv", {}).get("conflicts", [])
    pairs: list[set[str]] = []
    for group in conflicts:
        extras = {
            str(entry["extra"])
            for entry in group
            if isinstance(entry, dict) and "extra" in entry
        }
        if len(extras) > 1:
            pairs.append(extras)
    return pairs


def test_every_declared_conflict_names_real_extras() -> None:
    """A conflict that names an extra the project does not declare is dead config."""
    declared = _declared_extras()
    for pair in _declared_conflict_pairs():
        assert pair <= declared, (
            f"[tool.uv].conflicts names {sorted(pair - declared)} which is not an "
            f"extra of [project.optional-dependencies] {sorted(declared)}."
        )


def test_conflicting_extras_resolve_into_separate_lock_forks() -> None:
    """Each declared conflict must appear as two forks in the locked resolutions.

    Pre-fix the lock carries no ``extra ==`` resolution markers at all: uv
    resolves one environment in which every extra is installed, and the
    unsatisfiable pair makes every per-package upgrade fail. Post-fix uv emits
    a marker that selects one extra and excludes the other, which is the state
    Dependabot's ``uv lock --upgrade-package`` needs.
    """
    markers = _lock().get("resolution-markers", [])
    assert markers, "uv.lock has no resolution-markers; the lock cannot be read"

    pairs = _declared_conflict_pairs()
    assert pairs, (
        "[tool.uv].conflicts is empty: uv would resolve the cross product of "
        "every extra again and Dependabot's per-package upgrade of the shared "
        "dependencies would fail as in incident 4b896e55."
    )

    for pair in pairs:
        for extra in pair:
            other = pair - {extra}
            fork = [
                marker
                for marker in markers
                if extra in _EXTRA_MARKER.findall(marker)
                and other <= set(_EXCLUDED_MARKER.findall(marker))
            ]
            assert fork, (
                f"uv.lock has no resolution fork that selects the `{extra}` extra "
                f"while excluding {sorted(other)}: the declared conflict is not "
                "reflected in the lock, so a `uv lock --upgrade-package` for a "
                "package the extras disagree about will exit 1."
            )
