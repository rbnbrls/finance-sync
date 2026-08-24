#!/usr/bin/env sh
# Generate a cryptographically random 32-character ADMIN_KEY and persist it
# in the project .env file. Existing .env settings are preserved.
set -eu

if ! command -v openssl >/dev/null 2>&1; then
    printf '%s\n' 'openssl is required to generate ADMIN_KEY' >&2
    exit 1
fi

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file="${project_root}/.env"
admin_key=$(openssl rand -hex 16)
tmp_file=""
cleanup() {
    if [ -n "$tmp_file" ]; then
        rm -f "$tmp_file"
    fi
}
trap cleanup EXIT HUP INT TERM

if [ -f "$env_file" ]; then
    tmp_file=$(mktemp "${env_file}.tmp.XXXXXX")
    awk -v key="$admin_key" '
        BEGIN { updated = 0 }
        /^ADMIN_KEY=/ {
            print "ADMIN_KEY=" key
            updated = 1
            next
        }
        { print }
        END {
            if (!updated) print "ADMIN_KEY=" key
        }
    ' "$env_file" > "$tmp_file"
else
    tmp_file=$(mktemp "${env_file}.tmp.XXXXXX")
    printf 'ADMIN_KEY=%s\n' "$admin_key" > "$tmp_file"
fi

chmod 600 "$tmp_file"
mv "$tmp_file" "$env_file"
tmp_file=""

printf 'ADMIN_KEY=%s\n' "$admin_key"
