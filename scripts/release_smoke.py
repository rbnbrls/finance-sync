#!/usr/bin/env python3
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
    print(f"finance-sync release smoke tests — {BASE_URL}")
    print(f"  email: {EMAIL}")

    # 1. Liveness — the app is serving.
    print("[1/4] liveness (/health/live)")
    wait_for_health("/health/live")

    # 2. Readiness — DB + Redis reachable.
    print("[2/4] readiness (/health/ready)")
    wait_for_health("/health/ready")

    # 3. Authentication — login and obtain a bearer token.
    print("[3/4] authentication (POST /api/v1/auth/login)")
    status, body = request(
        "POST", "/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD}
    )
    if status != 200:
        sys.exit(f"✗ login failed (HTTP {status}): {json.dumps(body)[:200]}")
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        sys.exit(
            f"✗ login response has no access_token: {json.dumps(body)[:200]}"
        )
    print("  ✓ login OK (access_token obtained)")

    # 4. DB-backed read — GET /transactions returns the documented shape.
    print("[4/4] sync-read (GET /api/v1/transactions)")
    status, body = request("GET", "/api/v1/transactions", token=token)
    if status != 200:
        sys.exit(
            f"✗ GET /api/v1/transactions failed (HTTP {status}): {json.dumps(body)[:200]}"
        )
    if not isinstance(body, dict) or "meta" not in body:
        sys.exit(
            f"✗ GET /api/v1/transactions missing meta envelope: {json.dumps(body)[:200]}"
        )
    meta = body["meta"]
    expected_keys = {"as_of", "currency", "next_cursor", "freshness"}
    if not isinstance(meta, dict) or not expected_keys.issubset(meta):
        sys.exit(
            f"✗ GET /api/v1/transactions meta envelope wrong shape: {json.dumps(meta)[:200]}"
        )
    print(
        f"  ✓ GET /api/v1/transactions -> 200 (meta keys: {sorted(expected_keys)})"
    )

    print("✅ All staging smoke tests passed — promotion gate cleared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
