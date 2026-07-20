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

# Synchronise dependencies (no dev extras)
RUN uv sync --no-dev --frozen


# Production stage — minimal runtime image
# =========================================
FROM python:3.12-slim-bookworm AS production

# Install runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --system --gid 1000 finance && \
    useradd --system --gid finance --uid 1000 --create-home --shell /sbin/nologin finance

# Copy the project files and .venv from the build stage
WORKDIR /app
COPY --from=build /app /app
COPY --from=build /app/.venv /app/.venv

# Ensure /app/.venv/bin is on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENVIRONMENT=prod

# Healthcheck — uses the /health/live endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail http://localhost:8000/health/live || exit 1

# Drop privileges
USER finance

# Default command: run the FastAPI application via uvicorn
EXPOSE 8000
CMD ["uvicorn", "finance_sync.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
