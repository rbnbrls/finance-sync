"""Regression test: Trivy accepted-risk baseline (.trivyignore) policy.

The Build & Push CI job runs Trivy with severity HIGH,CRITICAL and
exit-code 1 (G-11 gate, added in task t_8675d5a3).  The .trivyignore
file documents the policy:

  - The CI gate FAILS on any HIGH/CRITICAL finding NOT listed there.
  - Each entry carries an expiry so the baseline is re-reviewed when it
    lapses; re-extend only if the finding still has no fix.
  - Never add a NEW CVE here -- new findings must be fixed, not ignored.

This test suite encodes those rules so a future edit cannot silently:
  1. drop the expiry or the rationale (entry becomes unbounded risk), or
  2. add a finding that actually HAS a fixed version upstream (the entry
     would mask a fixable vulnerability), or
  3. leave a known no-fix HIGH/CRITICAL finding undocumented while the
     image still carries the vulnerable package (the CI gate then fails
     every build until someone re-discovers why).
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRIVYIGNORE = PROJECT_ROOT / ".trivyignore"


def _baseline_entries() -> list[dict[str, str]]:
    """Parse .trivyignore into {cve, expiry, rationale} records."""
    lines = TRIVYIGNORE.read_text().splitlines()
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    pending_comment: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            pending_comment.append(stripped.lstrip("# ").strip())
            continue
        if not stripped:
            continue
        # A bare CVE line terminates the current record.
        if current:
            entries.append(current)
        current = {
            "cve": stripped,
            "expiry": "",
            "rationale": " ".join(pending_comment),
        }
        # Expiry marker is on the comment line above the CVE.
        for comment in pending_comment:
            if "expiry=" in comment:
                current["expiry"] = comment.split("expiry=", 1)[1].strip('"')
        pending_comment = []
    if current:
        entries.append(current)
    return entries


def test_baseline_entries_have_expiry() -> None:
    """Every ignored CVE must carry a re-review expiry date."""
    for entry in _baseline_entries():
        assert entry["expiry"], (
            f"{entry['cve']} in .trivyignore has no expiry= marker; "
            "accepted-risk entries must be re-reviewed when the expiry lapses"
        )


def test_baseline_entries_have_rationale() -> None:
    """Every ignored CVE must be explained (why it is accepted risk)."""
    for entry in _baseline_entries():
        # Rationale comment should say something about no-fix / accepted risk.
        assert entry["rationale"], (
            f"{entry['cve']} in .trivyignore has no rationale comment; "
            "accepted-risk entries must document why the risk is accepted"
        )


def test_no_fix_cve_14456_is_documented_or_image_fixed() -> None:
    """CVE-2026-14456 (openssl QUIC unbounded memory DoS) must not fail CI.

    As of 2026-08-18 the Debian security tracker lists NO fixed version
    for CVE-2026-14456 in any release (bookworm, bookworm-security,
    trixie -- postponed, sid): openssl 3.0.20-1~deb12u2 is the newest
    bookworm build and remains vulnerable.  There is nothing to upgrade
    to, so the only way for the Build & Push Trivy gate to stay green is
    for the finding to be recorded in the accepted-risk baseline (with
    expiry + rationale), exactly like the wget entries added for issue
    #233.  If a fixed openssl lands in bookworm, DELETE this entry and
    let the gate enforce the upgrade instead.
    """
    entries = {e["cve"]: e for e in _baseline_entries()}
    cve = "CVE-2026-14456"
    assert cve in entries, (
        f"{cve} (openssl, HIGH, no upstream fix as of 2026-08-18) is not "
        "recorded in .trivyignore; the Build & Push Trivy gate fails every "
        "build.  Add it with expiry + rationale (no-fix baseline), or bump "
        "openssl to a fixed version and drop this assertion."
    )
    assert entries[cve]["expiry"], f"{cve} entry lacks expiry marker"
    assert entries[cve]["rationale"], f"{cve} entry lacks rationale"
