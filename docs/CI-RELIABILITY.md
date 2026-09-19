# CI reliability and release governance

The repository provides a local fast gate and a full GitHub-parity gate. Run
the full gate before pushing a change that must pass all required checks:

```bash
uv sync --extra dev
make ci
```

`make ci-fast` runs, in order, Ruff format, Ruff lint, Pyright, test
collection, and the unit suite with the 80% coverage threshold. Collection is
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

## Release workflow

Changes go through a PR and must be validated locally before push. A normal
incident fix is a small, focused commit. Emergency break-glass access is an
administrator decision outside the normal release flow; any such change must
be followed by a PR reproducing the fix and a fresh green run on `main`.
