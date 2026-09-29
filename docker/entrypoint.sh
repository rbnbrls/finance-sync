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
  # Fail fast on a *permanent* misconfiguration before entering the retry
  # loop.  Without any database URL Alembic can never succeed — it raises
  # RuntimeError("No database URL configured...") on every attempt — so the
  # loop below only delays the failure by a minute and buries the cause under
  # one traceback per attempt.  The Coolify deployment of `financesync-test`
  # retried a missing URL 12 times and its log named no variable (incident
  # 2d643e85).  These are exactly the two variables `migrations/env.py` reads
  # (asyncpg normalisation happens there; only the presence matters here).
  #
  # A *reachable-but-unready* database is the transient case the loop exists
  # for, so it keeps its 12 attempts below.
  if [ -z "${ASYNC_DB_URL:-}" ] && [ -z "${DATABASE_URL:-}" ]; then
    log "ERROR: no database URL configured — set ASYNC_DB_URL (or DATABASE_URL) on this container."
    log "       Alembic cannot run without it; aborting startup instead of retrying a permanent misconfiguration."
    exit 1
  fi

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
