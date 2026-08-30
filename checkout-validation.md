# PR #547 checkout and holdout validation

Repository: `rbnbrls/finance-sync`
PR: https://github.com/rbnbrls/finance-sync/pull/547
Checked-out commit: `4993ffca86ace38528e1865ef5c35996880b2772`
Worktree: `/home/hermes/.hermes/kanban/boards/code/workspaces/t_88239c50/finance-sync`

The checkout is detached at the verified PR head. The coder branch was not
modified. The release backlog used for the test derivation is
`backlog/release19-loadtest-autoscaling.md`; the latest eight holdout scenarios
are summarized in the sibling artifact `holdout-scenarios-overview.md`.

## Commands and results

1. `uv sync --extra dev`
   - Exit 0; project environment resolved and installed.
2. `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -n auto -m "not integration and not e2e" --ignore=test_holdout_autoscaling.py`
   - Exit 0.
   - `3639 passed, 8 skipped, 180 warnings in 51.26s`.
3. `uv run ruff check test_holdout_autoscaling.py`
   - Exit 0 after formatting/import cleanup.
4. `uv run ruff format --check test_holdout_autoscaling.py`
   - Exit 0; file is formatted.
5. `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q test_holdout_autoscaling.py`
   - Exit 1, deliberately exposing one unmet holdout criterion.
   - 8 scenarios executed: 7 PASS, 1 FAIL.
   - Failing scenario: `Autoscaling-thrashing en afbouw met actieve leases`.
   - Evidence: no explicit hysteresis/cooldown or active-lease drain contract was
     found in this PR, so this criterion cannot be claimed as demonstrated.

## Holdout artifact

`test_holdout_autoscaling.py` is a hand-written executable harness containing
exactly the eight latest holdout scenarios. It uses synthetic tenant labels,
dummy secret markers, policy values, source-contract checks, and generated
load-test metrics; it does not use provider credentials, database services,
queue services, or financial data. It writes the machine-readable result to
`holdout-autoscaling-report.json`.

The harness is intentionally evidence-oriented: it reports an unmet criterion
as FAIL rather than converting absence of evidence into PASS. Its checks are
mostly deterministic contract/load-profile checks, not a substitute for live
multi-worker, provider, database, queue, or clock-fault testing. Numeric load
profiles and thresholds were not present in the source holdout comment, so the
values used by the harness are explicitly synthetic placeholders and should
not be interpreted as production capacity limits.
