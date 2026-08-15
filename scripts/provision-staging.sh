#!/usr/bin/env bash
# =============================================================================
# provision-staging.sh — Create the finance-sync staging stack in Coolify
# =============================================================================
# One-time / re-provisioning runbook for the staging environment used by the
# release pipeline (.github/workflows/release.yml).  Creates, in the
# finance-sync project's *development* environment on the homelab server:
#
#   1. finance-sync-staging-pg     (postgres:16-alpine, internal only)
#   2. finance-sync-staging-redis  (redis:7-alpine, internal only)
#   3. finance-sync-staging        (dockerfile build-pack app from
#                                   rbnbrls/finance-sync@main, port 8000,
#                                   /health/live, pre-deploy `alembic
#                                   upgrade head` against its own database)
#
# Usage:
#   COOLIFY_TOKEN=<bearer> ./scripts/provision-staging.sh
#
# Prerequisites: the Coolify API token (same one as the COOLIFY_API_TOKEN
# GitHub secret).  The script generates fresh random credentials for the
# staging databases and sets them on the app as environment variables, so it
# is safe to re-run if the stack was deleted (a new app UUID is produced —
# update the STAGING_APP_UUID in .github/workflows/release.yml afterwards).
#
# The script never touches the production application
# (obcopz3142hxzs1zlie78amh) or the production databases.
# =============================================================================
set -euo pipefail

BASE="${COOLIFY_BASE_URL:-https://dev.7rb.nl}"
TOKEN="${COOLIFY_TOKEN:?COOLIFY_TOKEN is required}"
PROJECT="ua2cwd0b6b9qof883tprcrdn"    # finance-sync
ENV_UUID="a8rrlq3u1k1otniyvhro16l3"   # development environment
SERVER="j4f9ldol13nscqx03ep91e7f"     # homelab (hosts finance-sync prod)
REPO="rbnbrls/finance-sync"

post() { # post <path> <json>
  curl -sS -X POST "${BASE}/api/v1$1" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$2"
}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " finance-sync — provision staging stack (Coolify ${BASE})"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

PG_PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
REDIS_PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
SECRET="$(openssl rand -base64 48 | tr -d '/+=' | head -c 48)"
MASTER="$(openssl rand -hex 32)"

echo "→ Creating staging PostgreSQL..."
PG_RESP="$(post /databases/postgresql "{
  \"server_uuid\": \"${SERVER}\",
  \"project_uuid\": \"${PROJECT}\",
  \"environment_uuid\": \"${ENV_UUID}\",
  \"name\": \"finance-sync-staging-pg\",
  \"image\": \"postgres:16-alpine\",
  \"postgres_user\": \"finance_sync\",
  \"postgres_password\": \"${PG_PW}\",
  \"postgres_db\": \"finance_sync\",
  \"instant_deploy\": true
}")"
PG_UUID="$(echo "${PG_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uuid',''))")"
[ -n "${PG_UUID}" ] || { echo "✗ postgres creation failed: ${PG_RESP}"; exit 1; }
echo "  ✓ ${PG_UUID}"

echo "→ Creating staging Redis..."
RD_RESP="$(post /databases/redis "{
  \"server_uuid\": \"${SERVER}\",
  \"project_uuid\": \"${PROJECT}\",
  \"environment_uuid\": \"${ENV_UUID}\",
  \"name\": \"finance-sync-staging-redis\",
  \"image\": \"redis:7-alpine\",
  \"redis_password\": \"${REDIS_PW}\",
  \"instant_deploy\": true
}")"
RD_UUID="$(echo "${RD_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uuid',''))")"
[ -n "${RD_UUID}" ] || { echo "✗ redis creation failed: ${RD_RESP}"; exit 1; }
echo "  ✓ ${RD_UUID}"

echo "→ Creating staging application (dockerfile build pack, public repo)..."
# NOTE: use the /applications/public route (git URL form) — the
# /applications/dockerfile route requires inline base64 Dockerfile content,
# which makes Coolify skip the git clone (empty build context, builds fail).
APP_RESP="$(curl -sS -X POST "${BASE}/api/v1/applications/public" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"server_uuid\": \"${SERVER}\",
    \"project_uuid\": \"${PROJECT}\",
    \"environment_uuid\": \"${ENV_UUID}\",
    \"name\": \"finance-sync-staging\",
    \"description\": \"Staging stack for the protected release pipeline (G-12)\",
    \"git_repository\": \"https://github.com/${REPO}\",
    \"git_branch\": \"main\",
    \"build_pack\": \"dockerfile\",
    \"ports_exposes\": \"8000\",
    \"health_check_enabled\": true,
    \"health_check_path\": \"/health/live\",
    \"health_check_port\": \"8000\",
    \"health_check_host\": \"localhost\",
    \"health_check_method\": \"GET\",
    \"health_check_return_code\": 200,
    \"health_check_scheme\": \"http\",
    \"health_check_interval\": 5,
    \"health_check_timeout\": 5,
    \"health_check_retries\": 10,
    \"health_check_start_period\": 15,
    \"pre_deployment_command\": \"alembic upgrade head\",
    \"instant_deploy\": false,
    \"is_auto_deploy_enabled\": false
  }")"
APP_UUID="$(echo "${APP_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uuid',''))")"
[ -n "${APP_UUID}" ] || { echo "✗ app creation failed: ${APP_RESP}"; exit 1; }
echo "  ✓ ${APP_UUID}"

echo "→ Setting staging app environment..."
set_env() {
  curl -sS -X POST "${BASE}/api/v1/applications/${APP_UUID}/envs" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"key\":\"$1\",\"value\":\"$2\",\"is_buildtime\":false,\"is_preview\":false}" >/dev/null
}
set_env APP_ENVIRONMENT "staging"
set_env APP_NAME "finance-sync-staging"
set_env DATABASE_URL "postgresql+asyncpg://finance_sync:${PG_PW}@${PG_UUID}:5432/finance_sync"
set_env REDIS_URL "redis://default:${REDIS_PW}@${RD_UUID}:6379/0"
set_env SECRET_KEY "${SECRET}"
set_env MASTER_ENCRYPTION_KEY "${MASTER}"
set_env LOG_LEVEL "INFO"
set_env CORS_ORIGINS '["*"]'
echo "  ✓ envs set"

echo ""
echo "✅ Staging stack provisioned:"
echo "  App UUID:      ${APP_UUID}  (update STAGING_APP_UUID in .github/workflows/release.yml)"
echo "  FQDN:          https://${APP_UUID}.7rb.nl"
echo "  PostgreSQL:    ${PG_UUID}"
echo "  Redis:         ${RD_UUID}"
echo ""
echo "Next: trigger a deploy (POST /api/v1/deploy {\"uuid\":\"${APP_UUID}\",\"force\":true})"
echo "and run the smoke tests: SMOKE_BASE_URL=https://${APP_UUID}.7rb.nl python3 scripts/release_smoke.py"
