.PHONY: install lint format type pyright-budget test test-cov coverage clean test-collect test-integration integration-up integration-down test-e2e e2e-up e2e-down migrations test-migrations security docker-ci openapi-diff ci-fast ci

# ── Setup ──────────────────────────────────────────────────────────
install:                           ## Install all dependencies (prod + dev)
	uv sync --extra dev

install-prod:                      ## Install production dependencies only
	uv sync --no-dev

.PHONY: uv-lock
uv-lock:                           ## Regenerate uv.lock from pyproject.toml
	uv lock

# ── Linting ────────────────────────────────────────────────────────
lint:                              ## Lint all Python files with Ruff
	uv run ruff check src tests

lint-fix:                          ## Lint and auto-fix
	uv run ruff check --fix src tests

format:                            ## Format all Python files with Ruff
	uv run ruff format src tests

format-check:                      ## Check formatting (CI use)
	uv run ruff format --check src tests

# ── Type checking ──────────────────────────────────────────────────
type:                              ## Type-check source and tests with CI's configs
	uv run pyright -p pyproject.toml src
	uv run pyright -p pyrightconfig.tests.json tests

type-ci:                           ## Type-check with Pyright in CI mode
	uv run pyright src tests --verifytypes finance_sync

pyright-budget:                    ## Enforce the repository Pyright warning budget
	uv run python scripts/check_pyright_budget.py --baseline config/pyright-warning-budget.json src

# ── Testing ────────────────────────────────────────────────────────
test:                              ## Run unit tests with pytest (excludes integration + e2e)
	APP_ENVIRONMENT=dev DEBUG=false uv run pytest -n auto -m "not integration and not e2e"

test-cov:                          ## Run unit tests with coverage report
	APP_ENVIRONMENT=dev DEBUG=false uv run pytest -n auto -m "not integration and not e2e" --cov=finance_sync --cov-report=term --cov-report=html

test-cov-xml:                      ## Run unit tests with XML coverage (CI)
	pytest -m "not integration and not e2e" --cov=finance_sync --cov-report=xml

test-collect:                      ## Collect every test without executing it
	uv run pytest --collect-only -q

test-ci:                           ## CI unit test run (sequential, coverage threshold)
	APP_ENVIRONMENT=dev DEBUG=false uv run pytest -m "not integration and not e2e" --cov=finance_sync --cov-report=term --cov-report=xml --cov-fail-under=80 --junitxml=junit.xml

ci-fast:                           ## Run the complete fast PR quality gate locally
	make format-check lint type pyright-budget test-collect test-ci

# ── Full GitHub CI parity ──────────────────────────────────────────
# These targets intentionally use the same commands and gates as
# .github/workflows/ci.yml. Service-backed checks fail when tests are skipped.
MIGRATION_DATABASE_URL ?= postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test
TEST_COMPOSE ?= docker compose -p finance-sync-ci -f docker-compose.test.yml

migrations:                         ## Run the GitHub migration round-trip locally
	ASYNC_DB_URL=$(MIGRATION_DATABASE_URL) uv run python scripts/check_migrations.py

test-migrations:                    ## Start PostgreSQL and run migration checks
	set -eu; $(TEST_COMPOSE) up -d --wait; \
	trap '$(TEST_COMPOSE) down' EXIT; \
	ASYNC_DB_URL=$(MIGRATION_DATABASE_URL) uv run python scripts/check_migrations.py

# ── Integration tests (real PostgreSQL + Redis) ─────────────────────
# Spins up ephemeral PG+Redis via docker compose and runs the
# `integration`-marked suite (tests/integration/) against them.
TEST_DATABASE_URL ?= postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test
TEST_REDIS_URL ?= redis://localhost:6380/15

integration-up:                    ## Start ephemeral PG + Redis for integration tests
	$(TEST_COMPOSE) up -d --wait

integration-down:                  ## Stop ephemeral integration services
	$(TEST_COMPOSE) down

test-integration:                  ## Run the integration suite (requires Docker)
	set -eu; trap 'make integration-down' EXIT; \
	make integration-up; \
	DEBUG=false TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) \
		uv run pytest -m integration -v --junitxml=junit-integration.xml; \
	uv run pytest tests/integration/test_read_query_benchmarks_pg.py -m integration -q; \
	uv run python scripts/check_junit_no_skips.py junit-integration.xml

# ── E2E tests (full app + worker + real PostgreSQL + Redis) ─────────
# Same ephemeral stack as the integration suite; runs the `e2e`-marked
# tests (tests/e2e/) that drive the API → outbox → worker pipeline and
# assert the exactly-once observable outcome (see README 'E2E tests').
e2e-up:                            ## Start ephemeral PG + Redis for e2e tests
	$(TEST_COMPOSE) up -d --wait

e2e-down:                          ## Stop ephemeral e2e services
	$(TEST_COMPOSE) down

test-e2e:                          ## Run the e2e suite (requires Docker)
	set -eu; trap 'make e2e-down' EXIT; \
	make e2e-up; \
	DEBUG=false TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) \
		uv run pytest -m e2e -v --junitxml=junit-e2e.xml; \
	uv run python scripts/check_junit_no_skips.py junit-e2e.xml

security:                           ## Run GitHub's dependency and policy gates locally
	uv pip install pip-audit cyclonedx-bom; \
	uv export --format requirements-txt --no-dev --locked --no-emit-project --output-file release-requirements.txt; \
	uv run pip-audit --requirement release-requirements.txt --progress-spinner=off \
		--ignore-vuln PYSEC-2026-1325 --ignore-vuln GHSA-wj6h-64fc-37mp; \
	uv run cyclonedx-py requirements release-requirements.txt --pyproject pyproject.toml -o sbom.cyclonedx.json; \
	uv run python scripts/check_trivyignore.py .trivyignore; \
	uv run python -m scripts.security_exception_report .trivyignore security-exceptions.json; \
	uv run python scripts/check_data_retention_policy.py config/data-retention-policy.json; \
	uv run python scripts/check_slo_alerts.py config/slo-alerts.json; \
	uv run python scripts/audit_trail_completeness.py --policy config/audit-trail-policy.json --example config/incident-audit-example.json

docker-ci:                          ## Build and scan the image like GitHub (without push)
	command -v trivy >/dev/null || { echo 'trivy is required for docker-ci (same as GitHub)'; exit 2; }; \
	docker build -t finance-sync:ci .; \
	trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed --ignorefile .trivyignore finance-sync:ci

BASE_REF ?= origin/main

openapi-diff:                       ## Compare OpenAPI with the PR merge-base
	set -eu; \
	repo_dir=$$(pwd); \
	base_dir=$$(mktemp -d); \
	trap 'rm -rf "$$base_dir" openapi-base.json openapi-head.json openapi-diff.json' EXIT; \
	git archive "$(BASE_REF)" | tar -x -C "$$base_dir"; \
	APP_ENVIRONMENT=dev DEBUG=false REDIS_URL=redis://localhost:6379/0 uv run python scripts/generate_openapi.py --output openapi-head.json; \
	(cd "$$base_dir" && APP_ENVIRONMENT=dev DEBUG=false REDIS_URL=redis://localhost:6379/0 uv run python scripts/generate_openapi.py --output "$$repo_dir/openapi-base.json"); \
	uv run python scripts/check_openapi_diff.py --base openapi-base.json \
		--head openapi-head.json --allowlist scripts/openapi_diff_allowlist.json \
		--report openapi-diff.json

ci:                                 ## Run all required GitHub CI gates locally
	make ci-fast test-migrations test-integration test-e2e security docker-ci

coverage:                          ## Generate HTML coverage report
	coverage html

# ── Housekeeping ───────────────────────────────────────────────────
clean:                             ## Remove cache and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytype -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ .coverage coverage.xml junit.xml

# ── Pre-commit ─────────────────────────────────────────────────────
pre-commit-install:                ## Install pre-commit hooks
	pre-commit install

pre-commit-run:                    ## Run pre-commit on all files
	pre-commit run --all-files

# ── SDK ────────────────────────────────────────────────────────────
SDK_DIR = sdks/finance-sync-sdk

sdk-install:                       ## Install SDK with dev dependencies
	cd $(SDK_DIR) && uv sync --extra dev

sdk-build:                         ## Build SDK distribution packages
	cd $(SDK_DIR) && python -m build

sdk-test:                          ## Run SDK tests
	cd $(SDK_DIR) && uv run pytest -v

sdk-lint:                          ## Lint SDK source
	cd $(SDK_DIR) && uv run ruff check src tests

sdk-format:                        ## Format SDK source
	cd $(SDK_DIR) && uv run ruff format src tests

# ── Docker / Deploy ────────────────────────────────────────────────
docker-build:                      ## Build Docker image
	docker build -t finance-sync:latest .

# ── Help ───────────────────────────────────────────────────────────
help:                              ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
