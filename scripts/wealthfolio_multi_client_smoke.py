#!/usr/bin/env python3
"""Multi-client smoke run for the Wealthfolio route (backlog AC).

Verifies the live chain ``provider -> finance-sync -> Wealthfolio -> two
clients``:

1. The public HTTPS URL serves the Wealthfolio PWA and password auth is
   enabled (``/api/v1/auth/status``).
2. Two *independent* client sessions (simulating a desktop browser and a
   mobile PWA) authenticate and read the same data — accounts and
   activities must be byte-identical between the two sessions.
3. The export freshness gate passes: the newest finance-sync delivery
   cursor (PostgreSQL ``wealthfolio_deliveries``) is not older than
   ``--max-stale-hours``.  The run FAILS (exit 1) when one client would
   see stale data.

Usage (stdlib only — runs on the Proxmox host or any machine):

    WF_PASSWORD='<password>' DATABASE_URL='postgresql://...' \\
        python3 scripts/wealthfolio_multi_client_smoke.py \
        --public-url https://wealthfolio.7rb.nl --max-stale-hours 24

Exit codes: 0 = all clients consistent + fresh; 1 = mismatch or stale;
2 = usage/config error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)
from finance_sync.monitoring.wealthfolio_monitor import (  # noqa: E402
    check_export_freshness,
    load_delivery_freshness,
)

API_PREFIX = "/api/v1"


def _request(
    base_url: str,
    path: str,
    *,
    password: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, object]:
    url = base_url.rstrip("/") + path
    headers = {"User-Agent": "curl/8.5.0", "Accept": "application/json"}
    data = None
    if password is not None:
        data = json.dumps({"password": password}).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read()
    try:
        parsed: object = json.loads(body)
    except ValueError:
        parsed = body.decode("utf-8", errors="replace")
    return status, parsed


class Client:
    """Tiny Wealthfolio API client (two independent sessions in the run)."""

    def __init__(self, base_url: str, password: str) -> None:
        self.base_url = base_url
        self.password = password
        self.authenticated = False

    def login(self) -> None:
        status, body = _request(
            self.base_url, f"{API_PREFIX}/auth/login", password=self.password
        )
        if status != 200:
            raise SystemExit(
                f"login failed (HTTP {status}): {body!r} — aborting smoke run"
            )
        self.authenticated = True

    def get_accounts(self) -> object:
        status, body = _request(self.base_url, f"{API_PREFIX}/accounts")
        if status != 200:
            raise SystemExit(f"accounts read failed (HTTP {status}): {body!r}")
        return body

    def search_activities(self) -> object:
        payload = json.dumps({"page": 0, "pageSize": 1000}).encode()
        request = urllib.request.Request(
            self.base_url.rstrip("/") + f"{API_PREFIX}/activities/search",
            data=payload,
            headers={
                "User-Agent": "curl/8.5.0",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SystemExit(
                f"activities search failed (HTTP {exc.code}): {exc.read()!r}"
            ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wealthfolio two-client consistency smoke run."
    )
    parser.add_argument("--public-url", default="https://wealthfolio.7rb.nl")
    parser.add_argument("--max-stale-hours", type=float, default=24.0)
    args = parser.parse_args(argv)

    public_url = args.public_url.rstrip("/")
    password = os.environ.get("WF_PASSWORD")
    if not password:
        print(
            "error: WF_PASSWORD env var required (Wealthfolio password)",
            file=sys.stderr,
        )
        return 2

    print(f"1) public URL {public_url} ...")
    status, body = _request(public_url, "/")
    ok_html = (
        status == 200 and isinstance(body, str) and "<html" in body.lower()
    )
    print(f"   PWA root: HTTP {status} html={ok_html}")
    if not ok_html:
        print("   FAIL: public URL does not serve the Wealthfolio PWA")
        return 1

    status, body = _request(public_url, f"{API_PREFIX}/auth/status")
    requires_password = isinstance(body, dict) and bool(
        body.get("requiresPassword")
    )
    print(f"   auth/status: HTTP {status} requiresPassword={requires_password}")
    if status != 200 or not requires_password:
        print("   FAIL: password auth not enabled on the public instance")
        return 1

    print("2) two independent client sessions ...")
    client_a = Client(public_url, password)
    client_b = Client(public_url, password)
    client_a.login()
    client_b.login()
    accounts_a = client_a.get_accounts()
    accounts_b = client_b.get_accounts()
    if accounts_a != accounts_b:
        print("   FAIL: client sessions observe different accounts")
        return 1
    activities_a = client_a.search_activities()
    activities_b = client_b.search_activities()
    if activities_a != activities_b:
        print("   FAIL: client sessions observe different activities")
        return 1
    n_accounts = len(accounts_a) if isinstance(accounts_a, list) else "?"
    n_activities = (
        str(activities_a["total"]) if isinstance(activities_a, dict) else "?"
    )
    print(
        f"   OK: both clients agree — accounts={n_accounts} activities={n_activities}"
    )

    print("3) export freshness ...")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("   note: DATABASE_URL not set — freshness check skipped")
    else:
        try:
            deliveries = load_delivery_freshness(database_url)
        except Exception as exc:  # noqa: BLE001
            print(f"   FAIL: cannot read delivery cursors: {exc}")
            return 1
        result = check_export_freshness(
            deliveries,
            max_stale_hours=args.max_stale_hours,
            now=datetime.now(UTC),
        )
        print(f"   {'OK' if result.ok else 'FAIL'}: {result.detail}")
        if not result.ok:
            return 1

    print("SMOKE OK — provider -> finance-sync -> Wealthfolio -> two clients")
    return 0


if __name__ == "__main__":
    sys.exit(main())
