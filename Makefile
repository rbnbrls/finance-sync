.PHONY: install lint format type test test-cov coverage clean test-integration integration-up integration-down test-e2e e2e-up e2e-down

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
	ruff check src tests

lint-fix:                          ## Lint and auto-fix
	ruff check --fix src tests

format:                            ## Format all Python files with Ruff
	ruff format src tests

format-check:                      ## Check formatting (CI use)
	ruff format --check src tests

# ── Type checking ──────────────────────────────────────────────────
type:                              ## Type-check with Pyright (strict)
	pyright src tests

type-ci:                           ## Type-check with Pyright in CI mode
	pyright src tests --verifytypes finance_sync

# ── Testing ────────────────────────────────────────────────────────
test:                              ## Run unit tests with pytest (excludes integration + e2e)
	pytest -n auto -m "not integration and not e2e"

test-cov:                          ## Run unit tests with coverage report
	pytest -n auto -m "not integration and not e2e" --cov=finance_sync --cov-report=term --cov-report=html

test-cov-xml:                      ## Run unit tests with XML coverage (CI)
	pytest -m "not integration and not e2e" --cov=finance_sync --cov-report=xml

test-ci:                           ## CI unit test run (sequential, coverage threshold)
	pytest -m "not integration and not e2e" --cov=finance_sync --cov-report=term --cov-report=xml --junitxml=junit.xml

# ── Integration tests (real PostgreSQL + Redis) ─────────────────────
# Spins up ephemeral PG+Redis via docker compose and runs the
# `integration`-marked suite (tests/integration/) against them.
TEST_DATABASE_URL ?= postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test
TEST_REDIS_URL ?= redis://localhost:6380/15

integration-up:                    ## Start ephemeral PG + Redis for integration tests
	docker compose -f docker-compose.test.yml up -d --wait

integration-down:                  ## Stop ephemeral integration services
	docker compose -f docker-compose.test.yml down

test-integration:                  ## Run the integration suite (requires Docker)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) \
		pytest -m integration -v

# ── E2E tests (full app + worker + real PostgreSQL + Redis) ─────────
# Same ephemeral stack as the integration suite; runs the `e2e`-marked
# tests (tests/e2e/) that drive the API → outbox → worker pipeline and
# assert the exactly-once observable outcome (see README 'E2E tests').
e2e-up:                            ## Start ephemeral PG + Redis for e2e tests
	docker compose -f docker-compose.test.yml up -d --wait

e2e-down:                          ## Stop ephemeral e2e services
	docker compose -f docker-compose.test.yml down

test-e2e:                          ## Run the e2e suite (requires Docker)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) \
		pytest -m e2e -v

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
