# CI reliability and release governance

The repository provides a local fast gate and a full GitHub-parity gate. Run
the full gate before pushing a change that must pass all required checks:

```bash
uv sync --extra dev
make ci
```

`make ci-fast` runs, in order, Ruff format, Ruff lint, Pyright, test
collection, and the unit suite with the 80% coverage threshold followed by the
published-coverage check (see "Published coverage summary"). Collection is
intentionally before test execution so missing imports or renamed helpers fail
without starting the expensive suite. `make ci` additionally runs the
PostgreSQL migration round-trip, real PostgreSQL/Redis integration and E2E
suites, dependency/policy checks, and the Docker image scan. It starts the
same PostgreSQL 16 and Redis 7 test services from `docker-compose.test.yml`.
Required integration and E2E suites fail when JUnit contains a skip, matching
the GitHub jobs; unavailable Docker or Trivy is a failed prerequisite rather
than an implicit pass.

For a pull request, run the public API compatibility check separately because
GitHub compares the PR head with its merge base:

```bash
BASE_REF=origin/main make openapi-diff
```

## Required checks on `main`

`main` requires a pull request, one approving review, approval from the latest
push, and current checks on the branch base. The required GitHub checks are:

- `Quality`
- `Lint (3.12)`
- `Type check (3.12)`
- `Test (3.12)`
- `Migrations`
- `Integration`
- `E2E`
- `Security`
- `Build & Push`

Stale approvals are dismissed, direct pushes and force-pushes are blocked, and
conversation resolution is required. The branch protection settings can be
verified without mutation with:

```bash
gh api repos/rbnbrls/finance-sync/branches/main/protection
```

The `Quality` job is the preflight gate for Integration and E2E. All test jobs
upload JUnit/log diagnostics and write a compact first-failure summary to the
GitHub Actions job summary, including the commit SHA. The incident workflow
reuses an open incident for the same workflow/job/branch/category across
repair pushes, while retaining the exact head SHA in the fingerprint and
issue details.

## Published coverage summary

`coverage-summary.json` at the repository root is committed, and it is the
number a reader outside CI can trust: a CI artifact expires, and a badge is an
image with no data in it. `scripts/coverage_report.py` derives it from the
per-line Cobertura report of the same run — one entry per measured file plus
the totals, with the per-file counters required to sum to the reporter's own
totals before anything is written.

The published artifact is a *summary* rather than the full report for a
concrete reason: the darkfactory quality lane reads a committed file through
the provider's contents API, which answers with an empty body above 1MB (and
the lane truncates what it does receive at 400,000 characters). The first
repair committed `coverage.xml` itself — 1.5MB — which made `main` look
measured while the lane still read nothing from it. `coverage.xml` therefore
stays a build output, uploaded as the `coverage-report-3.12` artifact, and the
committed summary is the reader-sized projection of it.
`[tool.coverage.run] relative_files` keeps every path in both files relative to
the checkout, and no run timestamp is carried, so the same measurement produces
the same bytes on any machine.

The `Test (3.12)` job measures, publishes, and then runs
`git diff --exit-code -- coverage-summary.json`: while that diff is dirty the
job fails, because a committed number nobody re-measures is a stale number and
a stale number is worse than no number. A change that moves coverage therefore
has to run `make coverage-refresh` and commit the regenerated summary in the
same pull request; an unchanged measurement produces no diff at all. The
workflow never commits the summary itself — the job token keeps
`contents: read`, so the publication travels through review and the merge.
`make ci-fast` runs the same check locally, and
`tests/test_coverage_report.py` pins the contract, including the size limit
that made the first attempt fail.

## Release workflow

Changes go through a PR and must be validated locally before push. A normal
incident fix is a small, focused commit. Emergency break-glass access is an
administrator decision outside the normal release flow; any such change must
be followed by a PR reproducing the fix and a fresh green run on `main`.
