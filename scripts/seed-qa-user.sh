#!/usr/bin/env bash
# =============================================================================
# seed-qa-user.sh — Create a QA test account for finance-sync
# =============================================================================
# Usage:
#   ./scripts/seed-qa-user.sh
#   ./scripts/seed-qa-user.sh -e user@example.com -p changeme
#   BASE_URL=http://localhost:8000 ./scripts/seed-qa-user.sh
#
# Default QA account credentials:
#   Email:    qa@finance-sync.local
#   Password: qatest123
#
# The script first checks if the user already exists (by attempting login).
# If not, it creates a new account via the registration endpoint.
# =============================================================================
set -euo pipefail

BASE_URL="${BASE_URL:-https://obcopz3142hxzs1zlie78amh.7rb.nl}"
EMAIL="qa@finance-sync.local"
PASSWORD="qatest123"
DISPLAY_NAME="QA Test User"

while getopts "e:p:n:h" opt; do
  case $opt in
    e) EMAIL="$OPTARG" ;;
    p) PASSWORD="$OPTARG" ;;
    n) DISPLAY_NAME="$OPTARG" ;;
    h)
      echo "Usage: $0 [-e email] [-p password] [-n display_name]"
      echo ""
      echo "Defaults:"
      echo "  Email:    qa@finance-sync.local"
      echo "  Password: qatest123"
      echo ""
      echo "Set BASE_URL env var for a different instance:"
      echo "  BASE_URL=http://localhost:8000 $0"
      exit 0
      ;;
    *) echo "Use -h for help"; exit 1 ;;
  esac
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " finance-sync — Seed QA User"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Instance:  ${BASE_URL}"
echo "  Email:     ${EMAIL}"
echo "  Password:  ${PASSWORD}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Check if user already exists by trying to log in ───────────────────
echo ""
echo "→ Checking if user already exists..."
LOGIN_RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST "${BASE_URL}/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"${EMAIL}\", \"password\": \"${PASSWORD}\"}" 2>/dev/null || true)

LOGIN_CODE=$(echo "${LOGIN_RESPONSE}" | tail -1)
LOGIN_BODY=$(echo "${LOGIN_RESPONSE}" | sed '$d')

if [ "${LOGIN_CODE}" = "200" ]; then
  echo "✓ User already exists — login succeeded!"
  echo ""
  echo "  Access token:  $(echo "${LOGIN_BODY}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"][:40] + "...")' 2>/dev/null || echo 'N/A')"
  exit 0
fi

# ── Register a new user ─────────────────────────────────────────────────
echo "→ Registering new user..."
REGISTER_RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST "${BASE_URL}/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"${EMAIL}\", \"password\": \"${PASSWORD}\", \"display_name\": \"${DISPLAY_NAME}\"}" 2>/dev/null || true)

REGISTER_CODE=$(echo "${REGISTER_RESPONSE}" | tail -1)
REGISTER_BODY=$(echo "${REGISTER_RESPONSE}" | sed '$d')

if [ "${REGISTER_CODE}" = "200" ]; then
  echo "✓ QA user created successfully!"
  echo ""
  echo "  Email:         ${EMAIL}"
  echo "  Password:      ${PASSWORD}"
  echo "  Display name:  ${DISPLAY_NAME}"
  echo "  User ID:       $(echo "${REGISTER_BODY}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["user"]["id"])' 2>/dev/null || echo 'N/A')"
  echo ""
  echo "  To log in via curl:"
  echo "    curl -X POST ${BASE_URL}/api/v1/auth/login \\"
  echo "      -H \"Content-Type: application/json\" \\"
  echo "      -d '{\"email\": \"${EMAIL}\", \"password\": \"${PASSWORD}\"}'"
else
  echo "✗ Registration failed (HTTP ${REGISTER_CODE})"
  echo "${REGISTER_BODY}" | python3 -m json.tool 2>/dev/null || echo "${REGISTER_BODY}"
  exit 1
fi
