#!/usr/bin/env sh
# =============================================================================
# Resolve a Coolify application by NAME and emit its identity + served route.
# =============================================================================
# Coolify application UUIDs are provider-internal and change whenever an
# application is recreated (issue #430: the production app was recreated twice,
# and the workflows kept deploying to a deleted UUID -> HTTP 404 on every push
# to main). Committing a UUID means every recreation silently breaks every
# release path, so nothing here is committed: the UUID is read back from the
# provider at run time and the workflow fails closed with a typed reason instead
# of deploying to whatever used to be there.
#
# Usage:
#   scripts/resolve-coolify-app.sh --name finance-sync-production [--prefix PROD_APP]
#
# Emits shell-assignable lines (append to "$GITHUB_ENV" in a workflow):
#   <PREFIX>_UUID=<uuid>
#   <PREFIX>_DOMAIN=https://<served-host>
#
# The served host is the Compose service domain Coolify actually routes (the
# service that carries traffic is not always called `web`; finance-sync
# publishes through `app`), falling back to the generated fqdn for a static
# application. Never the scheme-less UUID hostname of a Compose app, which
# serves no traffic.
#
# Environment:
#   COOLIFY_API_TOKEN        required (never printed)
#   COOLIFY_BASE_URL         default https://dev.7rb.nl
#   COOLIFY_APPLICATIONS_FILE  offline fixture: read /applications from a file
#                              instead of the provider (used by the tests)
#
# Exits non-zero with a typed `error:<reason>` line on stderr:
#   error:application_name_required
#   error:coolify_api_token_missing
#   error:coolify_api_unreachable:<base-url>
#   error:coolify_applications_unparsable
#   error:coolify_application_not_found:<name>
#   error:coolify_application_ambiguous:<name>:<uuid,uuid>
#   error:coolify_application_uuid_missing:<name>
#   error:coolify_domain_unresolved:<name>
# =============================================================================
set -eu

NAME=""
PREFIX="COOLIFY_APP"
BASE_URL="${COOLIFY_BASE_URL:-https://dev.7rb.nl}"

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "error:unknown_argument:$1" >&2; exit 2 ;;
  esac
done

if [ -z "$NAME" ]; then
  echo "error:application_name_required" >&2
  exit 2
fi

PAYLOAD=$(mktemp)
trap 'rm -f "$PAYLOAD"' EXIT INT TERM

if [ -n "${COOLIFY_APPLICATIONS_FILE:-}" ]; then
  if [ ! -f "$COOLIFY_APPLICATIONS_FILE" ]; then
    echo "error:coolify_applications_fixture_missing:$COOLIFY_APPLICATIONS_FILE" >&2
    exit 2
  fi
  cp "$COOLIFY_APPLICATIONS_FILE" "$PAYLOAD"
else
  if [ -z "${COOLIFY_API_TOKEN:-}" ]; then
    echo "error:coolify_api_token_missing" >&2
    exit 2
  fi
  if ! curl -sS -f --max-time 30 \
      -H "Authorization: Bearer ${COOLIFY_API_TOKEN}" \
      -H "Accept: application/json" \
      "${BASE_URL}/api/v1/applications" -o "$PAYLOAD"; then
    echo "error:coolify_api_unreachable:${BASE_URL}" >&2
    exit 1
  fi
fi

python3 - "$NAME" "$PREFIX" "$PAYLOAD" <<'PY'
import json
import sys

name, prefix, payload_path = sys.argv[1], sys.argv[2], sys.argv[3]


def fail(reason: str) -> None:
    print(f"error:{reason}", file=sys.stderr)
    raise SystemExit(1)


try:
    with open(payload_path, encoding="utf-8") as handle:
        applications = json.load(handle)
except (OSError, ValueError):
    fail("coolify_applications_unparsable")

if isinstance(applications, dict):
    # /applications returns a bare list; accept the common wrapped shapes too so
    # an API version bump reports a typed reason instead of a traceback.
    for key in ("applications", "data", "items"):
        if isinstance(applications.get(key), list):
            applications = applications[key]
            break
if not isinstance(applications, list):
    fail("coolify_applications_unparsable")

matches = [item for item in applications if isinstance(item, dict) and str(item.get("name")) == name]
if not matches:
    fail(f"coolify_application_not_found:{name}")
if len(matches) > 1:
    uuids = ",".join(sorted(str(item.get("uuid")) for item in matches))
    fail(f"coolify_application_ambiguous:{name}:{uuids}")

application = matches[0]
uuid = str(application.get("uuid") or "")
if not uuid:
    fail(f"coolify_application_uuid_missing:{name}")


def host(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("://", 1)[-1]
    return text.split("/", 1)[0].rstrip(".").lower()


compose_domains = application.get("docker_compose_domains")
if isinstance(compose_domains, str):
    try:
        compose_domains = json.loads(compose_domains) if compose_domains.strip() else {}
    except ValueError:
        compose_domains = {}
serving: dict[str, str] = {}
if isinstance(compose_domains, dict):
    for service, entry in compose_domains.items():
        if isinstance(entry, dict):
            value = host(entry.get("domain"))
            if value:
                serving[str(service)] = value

domain = serving.get("web")
if not domain and len(set(serving.values())) == 1:
    domain = next(iter(serving.values()))
if not domain:
    domain = host(application.get("fqdn"))
if not domain:
    fail(f"coolify_domain_unresolved:{name}")

print(f"{prefix}_UUID={uuid}")
print(f"{prefix}_DOMAIN=https://{domain}")
PY
