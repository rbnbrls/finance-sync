# syntax=docker/dockerfile:1
# Multi-stage Docker build for finance-sync
#
# Build stage — install dependencies with uv
# ===========================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

# Install system build deps (needed for some native extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project definition files first (layer caching)
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Alembic migration files (the Compose `migrate` service runs them before
# the application and worker containers start)
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY deploy/staging/fixtures/ ./deploy/staging/fixtures/

# Synchronise dependencies (no dev extras)
RUN uv sync --no-dev --frozen


# Production stage — minimal runtime image
# =========================================
FROM python:3.12-slim-trixie AS production

# Install runtime system deps.
# BOTH curl and wget are required (issue #233):
#   - curl backs the Dockerfile HEALTHCHECK below.
#   - wget is what Coolify's own healthcheck probe injects when Coolify
#     manages the healthcheck itself (custom_healthcheck_found=false).
#     A curl-only image made every probe fail with "wget: not found"
#     (rc 1), so Coolify rolled back every deployment.  Shipping wget
#     keeps the default probe working even if a Coolify-side custom
#     health_check_command is ever reset.
# python:*-slim includes these unused libraries; removing them keeps the
# runtime image clear of systemd/udev CVEs (CVE-2026-16742).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    ca-certificates \
    && apt-get purge -y --auto-remove libsystemd0 libudev1 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --system --gid 1000 finance && \
    useradd --system --gid finance --uid 1000 --create-home --shell /sbin/nologin finance

# Copy the project files and .venv from the build stage
WORKDIR /app
COPY --from=build /app /app
COPY --from=build /app/.venv /app/.venv

# Ensure the static files directory exists and is writable by the non-root user
RUN mkdir -p /app/src/finance_sync/static && chown -R finance:finance /app/src/finance_sync/static

# Ensure /app/.venv/bin is on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENVIRONMENT=prod

# Healthcheck — uses the /health/live endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail http://localhost:8000/health/live || exit 1

# Entrypoint: run alembic migrations before the app starts (issue #430).
# The Compose stack uses a dedicated `migrate` service for this; a
# single-container Coolify deployment has no such service and Coolify's
# pre_deployment_command is skipped on first deploy, so migrations run
# here instead.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Drop privileges
USER finance

# Default command: run the FastAPI application via uvicorn
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "finance_sync.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
