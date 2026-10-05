"""Every pip-audit gate must carry the same accepted-risk advisories.

`pip-audit` exits non-zero on any known advisory, including one that has no
fixed release to move to, so the advisories the repository has decided to
accept are passed as explicit ``--ignore-vuln`` flags. Four independent
execution sites run the scanner -- `ci.yml`'s Security job, `release.yml`'s
release security gate, the scheduled `Dependency cadence` workflow and the
`make security` target -- and each one carries its own copy of that flag list.

On 2026-10-05 the scheduled cadence run (run 37271582724, `Dependency cadence`
run #7) failed on `Found 2 known vulnerabilities in 1 package: ecdsa 0.19.2
PYSEC-2026-1325` while the other three sites stayed green on the very same
commit, because the cadence gate had been written without the flags. The
finding has no fixed version (0.19.2 *is* the latest python-ecdsa release, the
accepted risk is recorded in `.trivyignore`), so the gate can only be made
green by applying the same accepted-risk set everywhere.

The gate itself needs the live advisory database and cannot be reproduced
offline, so this file guards the offline half instead: every site that runs
pip-audit passes the same, still-present accepted-risk set, and a new site that
forgets them fails here -- on the pull request -- instead of in a weekly run
nobody reads.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Accepted-risk advisories passed to every pip-audit site. Add an entry only
#: with a rationale, an owner and an expiry in `.trivyignore`'s policy sense,
#: and add it to every site at once -- that is the invariant this file protects.
ACCEPTED_RISK_IGNORES: frozenset[str] = frozenset(
    {
        # python-ecdsa Minerva timing side channel: 0.19.2 is the latest
        # release, so there is no fixed version to pin a floor to.
        "PYSEC-2026-1325",
        # The same python-ecdsa advisory under its GitHub Advisory id.
        "GHSA-wj6h-64fc-37mp",
    }
)

#: Every file that invokes the scanner. Pinned so that a fifth execution site
#: cannot appear without an explicit decision about its accepted-risk flags.
PIP_AUDIT_SITES: tuple[str, ...] = (
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/workflows/dependency-cadence.yml",
    "Makefile",
)

_WORKFLOW_GLOB = ".github/workflows/*.yml"
_INVOCATION = re.compile(r"pip-audit\s+--")
_IGNORE_FLAG = re.compile(r"--ignore-vuln\s+([A-Za-z0-9][A-Za-z0-9._-]*)")


def _discovered_sites() -> dict[str, str]:
    """Map every file that invokes pip-audit to its text, newest surface first."""
    sites: dict[str, str] = {}
    for path in sorted(REPO_ROOT.glob(_WORKFLOW_GLOB)):
        text = path.read_text(encoding="utf-8")
        if _INVOCATION.search(text):
            sites[str(path.relative_to(REPO_ROOT))] = text
    makefile = REPO_ROOT / "Makefile"
    makefile_text = makefile.read_text(encoding="utf-8")
    if _INVOCATION.search(makefile_text):
        sites["Makefile"] = makefile_text
    return sites


def _ignored_advisories(text: str) -> set[str]:
    return {match.group(1) for match in _IGNORE_FLAG.finditer(text)}


def test_every_pip_audit_site_is_known() -> None:
    discovered = set(_discovered_sites())
    assert discovered == set(PIP_AUDIT_SITES), (
        "pip-audit execution sites changed: "
        f"discovered={sorted(discovered)} pinned={sorted(PIP_AUDIT_SITES)}. "
        "A new gate must pass the accepted-risk advisories and be added to "
        "PIP_AUDIT_SITES; a removed gate must disappear from both."
    )


def test_accepted_risk_set_is_not_empty() -> None:
    assert ACCEPTED_RISK_IGNORES, (
        "the accepted-risk set may only be emptied by fixing the advisories; "
        "an empty set turns every pip-audit gate red again."
    )


def test_every_site_passes_the_same_accepted_risks() -> None:
    sites = _discovered_sites()
    assert sites, "no pip-audit execution site found; the gate would never run"
    for relative_path, text in sites.items():
        passed = _ignored_advisories(text)
        missing = sorted(ACCEPTED_RISK_IGNORES - passed)
        extra = sorted(passed - ACCEPTED_RISK_IGNORES)
        assert not missing, (
            f"{relative_path} runs pip-audit without {missing}; the advisory "
            "is flagged in that gate while every other gate ignores it, which "
            "is exactly how the Dependency cadence run went red on 2026-10-05."
        )
        assert not extra, (
            f"{relative_path} ignores {extra}, which no other site accepts; "
            "add it to ACCEPTED_RISK_IGNORES (with a rationale) and to every "
            "site at once, or drop it here."
        )
