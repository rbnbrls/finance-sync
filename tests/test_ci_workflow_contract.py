import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_ci_has_fast_local_contract_and_non_pr_heavy_gates() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "workflow_dispatch:" in workflow
    assert 'cron: "17 4 * * 1-5"' in workflow
    assert workflow.count("./.github/actions/setup-python-uv") >= 6
    for job in (
        "provider-contracts",
        "connector-chaos",
        "backup-restore",
        "dr-game-day",
    ):
        assert (
            f"  {job}:\n    if: github.event_name != 'pull_request'" in workflow
        )
    assert "run: make format-check lint" in workflow
    assert "run: make type" in workflow
    assert "run: make test-ci" in workflow


def test_local_composite_action_is_checked_out_before_resolution() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for job_block in re.split(r"\n  (?=[A-Za-z0-9_-]+:\n)", workflow):
        if "uses: ./.github/actions/setup-python-uv" in job_block:
            steps = job_block.split("    steps:\n", 1)[1]
            action_position = steps.index(
                "- uses: ./.github/actions/setup-python-uv"
            )
            assert "- uses: actions/checkout@v7" in steps[:action_position]


def test_failure_workflow_is_main_only_and_supports_resolution() -> None:
    workflow = (ROOT / ".github/workflows/ci-failure.yml").read_text()
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch ==" in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "ci-failure-fingerprint:" in workflow
    assert "listWorkflowRunArtifacts" in workflow
    assert "**Branch:**" in workflow
    assert "scope:main" in workflow
    assert "state_reason: 'completed'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.conclusion == 'cancelled'" not in workflow
