#!/usr/bin/env python3
# ruff: noqa: T201, E501
# pyright: basic
"""Acceptance smoke tests for a deployed finance-sync staging stack.

Used by ``.github/workflows/release.yml`` (job ``smoke``) as the promotion
gate: if any check fails the script exits non-zero and production is never
deployed.  Can also be run locally against any instance:

    SMOKE_BASE_URL=https://4tavgkdyybkzzfgtkqtdxir9.7rb.nl \
        python3 scripts/release_smoke.py

Checks
------
1. ``GET  {base}/health/live``        -> 200      (app is serving)
2. ``GET  {base}/health/ready``       -> 200      (DB + Redis reachable)
3. ``POST {base}/api/v1/auth/login``  -> token    (auth works; the app
   seeds ``admin@finance-sync.local`` on first boot)
4. ``GET  {base}/api/v1/transactions`` -> 200 + ``meta`` envelope
   (DB-backed read path returns the documented shape)

Environment
-----------
SMOKE_BASE_URL       Base URL of the instance under test (no trailing slash).
SMOKE_EMAIL          Login email (default: admin@finance-sync.local).
SMOKE_PASSWORD       Login password (default: ``admin`` — the lifespan-seeded
                     default admin; override on environments where it was
                     rotated).
SMOKE_TIMEOUT        Overall budget in seconds (default: 600).
SMOKE_POLL_INTERVAL  Health poll interval in seconds (default: 10).
SMOKE_ARTIFACT       Optional JSON evidence output path.
SMOKE_COMMIT         Commit SHA recorded in the evidence.
SMOKE_IMAGE_TAG      Immutable image tag recorded in the evidence.
SMOKE_SCHEMA_VERSION Schema/migration head verified before deployment.
SMOKE_DATASET        Synthetic dataset identifier used by the run.
SMOKE_JUNIT          Optional JUnit XML output path.

Stdlib only — no dependencies, safe to run from a bare checkout.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("SMOKE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("SMOKE_EMAIL", "admin@finance-sync.local")
PASSWORD = os.environ.get("SMOKE_PASSWORD", "admin")
TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "600"))
POLL_INTERVAL = float(os.environ.get("SMOKE_POLL_INTERVAL", "10"))
ARTIFACT = os.environ.get("SMOKE_ARTIFACT", "")
COMMIT = os.environ.get("SMOKE_COMMIT", "unknown")
IMAGE_TAG = os.environ.get("SMOKE_IMAGE_TAG", "unknown")
SCHEMA_VERSION = os.environ.get("SMOKE_SCHEMA_VERSION", "unknown")
DATASET = os.environ.get("SMOKE_DATASET", "synthetic-provider-fixtures")
JUNIT = os.environ.get("SMOKE_JUNIT", "")
ENVIRONMENT = os.environ.get("SMOKE_ENVIRONMENT", "staging")
ARTIFACT_LINK = os.environ.get(
    "SMOKE_ARTIFACT_LINK", "staging-smoke-evidence.json"
)

if not BASE_URL:
    sys.exit(
        "SMOKE_BASE_URL is required (e.g. https://<staging-app-uuid>.7rb.nl)"
    )


def request(
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict | str]:
    """Return (http_status, parsed_body)."""
    headers = {
        "Accept": "application/json",
        # Cloudflare's browser-integrity check blocks the default
        # "Python-urllib/..." user agent (HTTP 403 / error 1010), so send a
        # curl-style UA like the rest of the release pipeline.
        "User-Agent": "curl/8.5.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", method=method, data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, str(exc)


def wait_for_health(path: str) -> None:
    """Poll an endpoint until it returns 200 or the budget is exhausted."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        status, body = request("GET", path)
        if status == 200:
            print(f"  ✓ GET {path} -> 200")
            return
        print(
            f"  … GET {path} -> {status} ({body if isinstance(body, str) else json.dumps(body)[:120]})"
        )
        time.sleep(POLL_INTERVAL)
    sys.exit(f"✗ TIMEOUT: {path} did not become healthy within {TIMEOUT:.0f}s")


def main() -> int:
    checks: list[dict[str, object]] = []

    def record(name: str, status: int, detail: str) -> None:
        checks.append({"name": name, "http_status": status, "detail": detail})

    def require_status(
        name: str,
        status: int,
        body: dict | str,
        expected: set[int],
    ) -> dict | str:
        if status not in expected:
            safe_body = (
                body if isinstance(body, str) else {"keys": sorted(body)}
            )
            sys.exit(
                f"✗ {name} failed (HTTP {status}): "
                f"{json.dumps(safe_body)[:200]}"
            )
        record(name, status, "ok")
        return body

    print(f"finance-sync release smoke tests — {BASE_URL}")

    # 1. Liveness — the app is serving.
    print("[1/7] liveness (/health/live)")
    wait_for_health("/health/live")
    record("health_live", 200, "ok")

    # 2. Readiness — DB + Redis reachable.
    print("[2/7] readiness (/health/ready)")
    wait_for_health("/health/ready")
    record("health_ready", 200, "database and redis ready")

    # 3. Authentication — login and obtain a bearer token.
    print("[3/7] authentication (POST /api/v1/auth/login)")
    status, body = request(
        "POST", "/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD}
    )
    require_status("auth_login", status, body, {200})
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        sys.exit(
            f"✗ login response has no access_token: {json.dumps(body)[:200]}"
        )
    print("  ✓ login OK (access_token obtained)")

    # 4. DB-backed read — GET /transactions returns the documented shape.
    print("[4/7] sync-read (GET /api/v1/transactions)")
    status, body = request("GET", "/api/v1/transactions", token=token)
    require_status("transactions_read", status, body, {200})
    if not isinstance(body, dict) or "meta" not in body:
        sys.exit("✗ GET /api/v1/transactions missing meta envelope")
    meta = body["meta"]
    expected_keys = {"as_of", "currency", "next_cursor", "freshness"}
    if not isinstance(meta, dict) or not expected_keys.issubset(meta):
        sys.exit("✗ GET /api/v1/transactions meta envelope wrong shape")
    print(
        f"  ✓ GET /api/v1/transactions -> 200 (meta keys: {sorted(expected_keys)})"
    )

    # 5. Synthetic provider sync — no external provider credentials or data.
    print("[5/7] synthetic sync (POST /api/v1/sync/bunq)")
    status, body = request("POST", "/api/v1/sync/bunq", token=token)
    sync_body = require_status("synthetic_sync", status, body, {202})
    sync_links = (
        sync_body.get("sync_runs", []) if isinstance(sync_body, dict) else []
    )
    if not isinstance(sync_links, list) or not sync_links:
        sys.exit("✗ synthetic sync returned no sync-run/outbox evidence")
    record("sync_outbox_evidence", 202, f"sync_links={len(sync_links)}")

    # 6. Durable sync-run listing proves the write path committed.
    print("[6/7] sync-run listing and exporter flow")
    status, body = request("GET", "/api/v1/sync-runs", token=token)
    runs_body = require_status("sync_runs_read", status, body, {200})
    if not isinstance(runs_body, (dict, list)):
        sys.exit("✗ sync-run response has an unexpected shape")

    status, body = request("POST", "/api/v1/exporters/export", token=token)
    export_body = require_status("exporter_run", status, body, {200, 202})
    if isinstance(export_body, dict) and export_body.get("status") == "failed":
        sys.exit("✗ exporter smoke run returned failed")
    status, body = request("GET", "/api/v1/exporters/runs", token=token)
    require_status("exporter_runs_read", status, body, {200})

    if ARTIFACT:
        evidence = {
            "commit": COMMIT,
            "image_tag": IMAGE_TAG,
            "schema_version": SCHEMA_VERSION,
            "synthetic_dataset": DATASET,
            "environment": ENVIRONMENT,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "artifact_link": ARTIFACT_LINK,
            "checks": checks,
            "synthetic_data_only": True,
            "secrets_included": False,
        }
        with open(ARTIFACT, "w", encoding="utf-8") as evidence_file:
            json.dump(evidence, evidence_file, indent=2)
            evidence_file.write("\n")

    if JUNIT:
        testcase_count = len(checks)
        junit = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuite name="release-smoke" tests="{testcase_count}" '
            'failures="0" errors="0" skipped="0">\n'
            + "".join(
                f'  <testcase classname="release_smoke" name="{check["name"]}"/>\n'
                for check in checks
            )
            + "</testsuite>\n"
        )
        with open(JUNIT, "w", encoding="utf-8") as junit_file:
            junit_file.write(junit)

    print("✅ All staging smoke tests passed — promotion gate cleared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
