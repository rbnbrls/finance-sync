"""Pure CI failure classification and fingerprinting helpers.

The GitHub workflow contains the API orchestration; these helpers keep the
classification contract deterministic and testable without GitHub access.
"""

from __future__ import annotations

import re

IGNORED_CONCLUSIONS = {"cancelled", "skipped", "neutral", "success"}


def normalize(value: str) -> str:
    """Return a stable marker-safe value."""
    return (
        re.sub(r"[^a-z0-9_.-]+", "-", value.strip().lower()).strip("-")
        or "unknown"
    )


def failure_category(job_name: str, step_name: str = "") -> str:
    """Classify a failed job without relying on a fixed job-name vocabulary."""
    value = f"{job_name} {step_name}".lower()
    if re.search(r"security|trivy|audit|sbom", value):
        return "security"
    if re.search(r"lint|ruff|format", value):
        return "lint"
    if re.search(r"test|pytest|integration|e2e", value):
        return "test"
    if re.search(r"build|docker|image", value):
        return "build"
    if re.search(r"deploy|release|publish", value):
        return "release"
    return "other"


def is_reportable(
    *, event: str, branch: str, default_branch: str, conclusion: str
) -> bool:
    """Only report failures from the default branch, never PR noise."""
    return (
        event == "push"
        and branch == default_branch
        and conclusion.lower() not in IGNORED_CONCLUSIONS
    )


def fingerprint(
    *, workflow: str, job: str, default_branch: str, category: str
) -> str:
    """Create the stable deduplication key used by incident issues."""
    return "|".join(
        (
            normalize(workflow),
            normalize(job),
            normalize(default_branch),
            normalize(category),
        )
    )
