.PHONY: install lint format type test test-cov coverage clean

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
test:                              ## Run tests with pytest
	pytest -n auto

test-cov:                          ## Run tests with coverage report
	pytest -n auto --cov=finance_sync --cov-report=term --cov-report=html

test-cov-xml:                      ## Run tests with XML coverage (CI)
	pytest -n auto --cov=finance_sync --cov-report=xml

test-ci:                           ## CI test run (sequential, coverage threshold)
	pytest --cov=finance_sync --cov-report=term --cov-report=xml --junitxml=junit.xml

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
