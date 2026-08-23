#!/usr/bin/env bash
set -euo pipefail

repo_dir="${SECURO_SOURCE_DIR:-$PWD/var/securo-test}"
repo_url="${SECURO_REPO_URL:-https://github.com/securo-finance/securo.git}"

if [[ ! -d "$repo_dir/backend" || ! -d "$repo_dir/frontend" ]]; then
  mkdir -p "$(dirname "$repo_dir")"
  git clone --depth 1 "$repo_url" "$repo_dir"
fi

docker compose -f docker-compose.securo.yml up -d --build
echo "Securo draait op http://localhost:${SECURO_FRONTEND_PORT:-3001}"
echo "Maak daar een lokale gebruiker aan; gebruik daarna SECURO_EMAIL en SECURO_PASSWORD voor 'finance-sync securo push'."
