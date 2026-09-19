from scripts.ci_failure_policy import (
    failure_category,
    fingerprint,
    is_reportable,
)


def test_failure_category_is_robust_for_unrecognised_job_names() -> None:
    assert failure_category("quality gate", "ruff format") == "lint"
    assert failure_category("container scan", "Trivy") == "security"
    assert failure_category("mystery job", "unexpected step") == "other"


def test_only_default_branch_push_failures_are_reportable() -> None:
    assert is_reportable(
        event="push", branch="main", default_branch="main", conclusion="failure"
    )
    assert not is_reportable(
        event="pull_request",
        branch="feature/x",
        default_branch="main",
        conclusion="failure",
    )
    assert not is_reportable(
        event="push", branch="main", default_branch="main", conclusion="skipped"
    )


def test_fingerprint_is_stable_and_contains_job_scope() -> None:
    value = fingerprint(
        workflow="CI",
        job="Integration / PG",
        default_branch="main",
        category="test",
    )
    assert value == "ci|integration-pg|main|test"
