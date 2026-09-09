#!/usr/bin/env python3
# ruff: noqa: T201
"""Record sanitized authenticated fixtures from the live Wealthfolio instance.

Fetches the instance password at runtime from LXC 104 (never prints it),
authenticates, then captures the real API responses for:

  - GET  /api/v1/accounts            (authenticated)
  - POST /api/v1/activities/search   (authenticated, smoke account)
  - GET  /api/v1/holdings/list       (authenticated, smoke account)

Responses are stored as ``{status_code, body}`` fixture files (same shape
as the existing live fixtures in tests/exporter/fixtures/live/).  Session
cookies and any credential material are discarded; only the JSON response
body is recorded.  Run from the repo root:

    .venv/bin/python scripts/record_wealthfolio_live_fixtures.py

Production-write guard: this script talks to the live instance.  The
fixture smoke account (``Smoke Test Brokerage``) caused a production
incident (issue #504: a NULL-asset BUY row spun the holdings
recalculation past Wealthfolio's own 30s request cap and produced HTTP
408s).  Recording fixtures against the production instance is therefore
opt-in: pass ``--allow-prod`` when the target is the real instance, and
only read-only endpoints may be recorded (this script never POSTs data).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

BASE_URL = "http://192.168.3.50:8080"
API = f"{BASE_URL}/api/v1"
FIXTURE_DIR = Path("tests/exporter/fixtures/live")
# The production instance (LXC 104, wealthfolio.7rb.nl).  Recording
# fixtures from it requires --allow-prod (issue #504 regression guard).
PROD_BASE_URLS = {
    "http://192.168.3.50:8080",
    "https://wealthfolio.7rb.nl",
    "http://wealthfolio.7rb.nl",
}


def fetch_password() -> str:
    """Read the Wealthfolio instance password from LXC 104 at runtime."""
    proc = subprocess.run(
        [
            "/home/hermes/.hermes/scripts/proxmox-exec104.py",
            "cat /root/wealthfolio.creds",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    m = re.search(r"WF_PASSWORD=(.+)", proc.stdout)
    if not m:
        msg = "password not found on LXC 104"
        raise RuntimeError(msg)
    return m.group(1).strip()


def save_fixture(name: str, response: httpx.Response) -> None:
    """Persist ``{status_code, body}`` for a recorded response."""
    try:
        body = response.json()
    except ValueError:
        body = response.text
    payload = {"status_code": response.status_code, "body": body}
    out = FIXTURE_DIR / f"{name}.json"
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {out.name} ({response.status_code})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Record sanitized live fixtures from a Wealthfolio instance."
        )
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help=(
            "Allow recording from the production instance (LXC 104 / "
            "wealthfolio.7rb.nl).  Required because the fixture smoke "
            "account caused issue #504 (HTTP 408 on slow snapshot POSTs)."
        ),
    )
    args = parser.parse_args()

    if BASE_URL.rstrip("/") in PROD_BASE_URLS and not args.allow_prod:
        print(
            "Refusing to record fixtures from the production Wealthfolio "
            "instance without --allow-prod (issue #504 regression guard).",
            file=sys.stderr,
        )
        sys.exit(2)

    password = fetch_password()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=BASE_URL, timeout=20.0) as client:
        # Auth status (unauthenticated, re-recorded for freshness)
        r = client.get(f"{API}/auth/status")
        save_fixture("auth_status", r)

        # Login (200 only; body/cookies discarded)
        r = client.post(f"{API}/auth/login", json={"password": password})
        if r.status_code != 200:
            msg = f"login failed: HTTP {r.status_code}"
            raise RuntimeError(msg)
        print("  authenticated OK (cookie kept in-memory only)")

        # Authenticated accounts list
        r = client.get(f"{API}/accounts")
        save_fixture("accounts_auth", r)
        accounts = r.json()

        # Resolve the finance-sync smoke account from the live accounts list
        smoke = next(
            (
                acc
                for acc in accounts
                if str(acc.get("provider") or "").upper() == "FINANCE_SYNC"
                and "smoke" in str(acc.get("name") or "").lower()
            ),
            None,
        )
        if smoke is None:
            msg = "finance-sync smoke account not found in live accounts"
            raise RuntimeError(msg)
        smoke_id = str(smoke["id"])
        print(f"  resolved smoke account {smoke_id!r}")

        # Authenticated activities search (smoke account)
        r = client.post(
            f"{API}/activities/search",
            json={
                "page": 0,
                "pageSize": 1000,
                "accountIdFilter": smoke_id,
            },
        )
        save_fixture("activities_search", r)

        # Authenticated holdings list (smoke account)
        r = client.get(f"{API}/holdings/list", params={"accountId": smoke_id})
        save_fixture("holdings_list", r)


if __name__ == "__main__":
    main()
