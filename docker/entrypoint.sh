#!/usr/bin/env bash
# finance-sync container entrypoint.
#
# Runs database migrations (alembic upgrade head) before starting the
# application.  In the Compose stack this is the dedicated `migrate`
# service; in a single-container Coolify deployment there is no such
# service and Coolify's pre_deployment_command is skipped on the first
# deployment ("No running containers found"), so migrations must run at
# container start (issue #430).
#
# The DATABASE_URL check is deliberately retried: on the first deploy of a
# fresh Coolify app the database resource may still be provisioning, and a
# short-lived startup failure must not permanently crash the container.
set -euo pipefail

log() { echo "[entrypoint] $*"; }

# Allow skipping migrations (e.g. worker image or one-off debugging).
if [ "${SKIP_MIGRATIONS:-0}" = "1" ]; then
  log "SKIP_MIGRATIONS=1 — skipping alembic upgrade head"
else
  # Wait up to ~60s for the database to become reachable.
  for i in $(seq 1 12); do
    if alembic upgrade head; then
      log "alembic upgrade head completed"
      break
    fi
    log "alembic upgrade head failed (attempt ${i}/12) — retrying in 5s..."
    sleep 5
    if [ "$i" -eq 12 ]; then
      log "alembic upgrade head failed after 12 attempts; aborting startup"
      exit 1
    fi
  done
fi

log "starting: $*"
exec "$@"
