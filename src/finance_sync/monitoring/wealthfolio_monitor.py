"""Wealthfolio multi-device access monitor.

Checks, for the self-hosted Wealthfolio instance (backlog story
"veilige-multi-device-toegang-wealthfolio"):

* HTTPS reachability of the public URL (``https://wealthfolio.7rb.nl``).
* TLS certificate expiry (warns before the Let's Encrypt renewal window).
* Wealthfolio health (``/api/v1/auth/status`` responds, password auth
  enabled, no OIDC surprise).
* Freshness of the bunq/Trading212 export: age of the newest
  ``wealthfolio_deliveries`` cursor (finance-sync PostgreSQL).  Stale
  exports fail the check.

Metrics are exported in Prometheus text format with **no financial values
and no secrets in labels** — labels are limited to the fixed check names.

Run standalone (no Hermes)::

    finance-sync-wealthfolio-monitor --public-url https://wealthfolio.7rb.nl \
        --database-url "$DATABASE_URL" --max-stale-hours 24

Exit code 1 on any critical failure, 2 on config errors.  Prometheus
output on stdout, JSON status on stderr when ``--json`` is given.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError

DEFAULT_PUBLIC_URL = "https://wealthfolio.7rb.nl"
CERT_WARN_DAYS = 21
DEFAULT_MAX_STALE_HOURS = 24.0
SSE_HEALTH_PATH = "/api/v1/auth/status"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single monitor check."""

    name: str
    ok: bool
    detail: str = ""
    value: float | None = None  # numeric value (age hours, days left, ...)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "value": self.value,
        }


# ═══════════════════════════════════════════════════════════════════════
# HTTP / TLS checks
# ═══════════════════════════════════════════════════════════════════════


def _http_get(url: str, timeout: float = 15.0) -> tuple[int, bytes]:
    """GET *url*, returning ``(status_code, body)``.

    Raises ``URLError``/``HTTPError``/``OSError`` on failure (callers
    convert to a failed ``CheckResult``).
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "finance-sync-wealthfolio-monitor/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def check_https_reachable(
    public_url: str, timeout: float = 15.0
) -> CheckResult:
    """Check the public HTTPS URL serves the Wealthfolio SPA."""
    url = public_url.rstrip("/") + "/"
    try:
        status, body = _http_get(url, timeout=timeout)
    except (URLError, HTTPError, OSError) as exc:
        return CheckResult(
            "https_reachable",
            False,
            detail=f"{url}: {exc.__class__.__name__}: {exc}",
        )
    is_html = (
        b"<html" in body[:4096].lower() or b"<!doctype" in body[:4096].lower()
    )
    ok = status == 200 and is_html
    return CheckResult(
        "https_reachable",
        ok,
        detail=f"{url} -> HTTP {status} (html={is_html})",
    )


def check_cert_expiry(
    public_url: str, warn_days: int = CERT_WARN_DAYS, timeout: float = 15.0
) -> CheckResult:
    """Check the TLS certificate of *public_url* does not expire soon."""
    host = urllib.parse.urlsplit(public_url).hostname
    if not host:
        return CheckResult("cert_expiry", False, detail="no hostname in URL")
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(
            f"https://{host}/", timeout=timeout, context=context
        ) as response:
            cert = response.getpeercert()
    except (URLError, HTTPError, OSError, ssl.SSLError) as exc:
        return CheckResult(
            "cert_expiry",
            False,
            detail=f"cannot read cert: {exc.__class__.__name__}: {exc}",
        )
    if not cert:
        return CheckResult("cert_expiry", False, detail="no peer certificate")
    not_after = datetime.strptime(
        cert["notAfter"], "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=UTC)
    days_left = (not_after - datetime.now(UTC)).total_seconds() / 86400.0
    return CheckResult(
        "cert_expiry",
        days_left > warn_days,
        detail=(
            f"cert expires {not_after.isoformat()} "
            f"({days_left:.1f} days left, warn < {warn_days})"
        ),
        value=round(days_left, 1),
    )


def check_wealthfolio_health(
    public_url: str, timeout: float = 15.0
) -> CheckResult:
    """Check the Wealthfolio auth-status endpoint.

    Password auth must be enabled (``requiresPassword`` true) — this
    catches a deployment where the password was accidentally removed.
    """
    url = public_url.rstrip("/") + SSE_HEALTH_PATH
    try:
        status, body = _http_get(url, timeout=timeout)
    except (URLError, HTTPError, OSError) as exc:
        return CheckResult(
            "wealthfolio_health",
            False,
            detail=f"{url}: {exc.__class__.__name__}: {exc}",
        )
    try:
        payload = json.loads(body)
    except ValueError:
        return CheckResult(
            "wealthfolio_health",
            False,
            detail=f"{url}: HTTP {status}, non-JSON body",
        )
    requires_password = bool(payload.get("requiresPassword"))
    oidc_enabled = bool(payload.get("oidcEnabled"))
    ok = status == 200 and requires_password
    return CheckResult(
        "wealthfolio_health",
        ok,
        detail=(
            f"{url}: HTTP {status}, requiresPassword={requires_password}, "
            f"oidcEnabled={oidc_enabled}"
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
# Export freshness (finance-sync delivery cursors)
# ═══════════════════════════════════════════════════════════════════════


def normalize_pg_url(database_url: str) -> str:
    """Map an async SQLAlchemy URL to a psycopg-compatible one."""
    if database_url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + database_url[len("postgresql+asyncpg://") :]
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url[len("postgresql+psycopg://") :]
    return database_url


def load_delivery_freshness(database_url: str) -> list[tuple[datetime, str]]:
    """Load ``(last_exported_at, account_id)`` for every delivery cursor.

    Returns an empty list when the table is empty or unreachable (callers
    treat an empty list as "no data yet" — see ``check_export_freshness``).
    """
    import psycopg  # pyright: ignore[reportMissingImports]

    url = normalize_pg_url(database_url)
    conn = psycopg.connect(url, connect_timeout=10)
    try:
        raw_rows: Any = conn.execute(
            "SELECT last_exported_at, account_id "
            "FROM wealthfolio_deliveries "
            "WHERE last_exported_at IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    result: list[tuple[datetime, str]] = []
    for raw in raw_rows:
        ts = raw[0]
        if ts is not None:
            result.append((ts, str(raw[1])))
    return result


def check_export_freshness(
    deliveries: list[tuple[datetime, str]],
    max_stale_hours: float = DEFAULT_MAX_STALE_HOURS,
    now: datetime | None = None,
) -> CheckResult:
    """Check the newest delivery cursor is not older than *max_stale_hours*.

    ``deliveries`` is a list of ``(last_exported_at, account_id)`` tuples.
    An empty list means finance-sync has never pushed anything — that is a
    freshness failure (no export at all).
    """
    now = now or datetime.now(UTC)
    if not deliveries:
        return CheckResult(
            "export_freshness",
            False,
            detail="no wealthfolio delivery cursors — nothing exported yet",
        )
    newest = max(dt for dt, _ in deliveries)
    age_hours = (now - newest).total_seconds() / 3600.0
    ok = age_hours <= max_stale_hours
    return CheckResult(
        "export_freshness",
        ok,
        detail=(
            f"newest delivery {newest.isoformat()} "
            f"({age_hours:.1f}h old, max {max_stale_hours:.1f}h)"
        ),
        value=round(age_hours, 2),
    )


# ═══════════════════════════════════════════════════════════════════════
# Prometheus rendering (no financial values / secrets in labels)
# ═══════════════════════════════════════════════════════════════════════


def render_prometheus(results: list[CheckResult]) -> str:
    """Render checks as Prometheus text format.

    Labels are limited to the fixed ``check`` name — no financial values,
    no secrets, no user-controlled strings.
    """
    lines = [
        "# HELP wealthfolio_check_status 1 if the check passes, 0 otherwise.",
        "# TYPE wealthfolio_check_status gauge",
    ]
    for result in results:
        lines.append(
            f'wealthfolio_check_status{{check="{result.name}"}} '
            f"{1 if result.ok else 0}"
        )
        if result.value is not None:
            lines.append(
                f'wealthfolio_check_value{{check="{result.name}"}} '
                f"{result.value}"
            )
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════════════════════


def run_all_checks(
    public_url: str,
    max_stale_hours: float,
    database_url: str | None,
    now: datetime | None = None,
) -> list[CheckResult]:
    """Run every check; DB-dependent checks degrade gracefully."""
    results = [
        check_https_reachable(public_url),
        check_cert_expiry(public_url),
        check_wealthfolio_health(public_url),
    ]
    if database_url:
        try:
            deliveries = load_delivery_freshness(database_url)
        except Exception as exc:
            results.append(
                CheckResult(
                    "export_freshness",
                    False,
                    detail=(
                        f"database unreachable: {exc.__class__.__name__}: {exc}"
                    ),
                )
            )
        else:
            results.append(
                check_export_freshness(deliveries, max_stale_hours, now)
            )
    else:
        results.append(
            CheckResult(
                "export_freshness",
                False,
                detail=(
                    "database_url not configured — freshness check "
                    "skipped (fail closed)"
                ),
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wealthfolio multi-device access monitor."
    )
    parser.add_argument("--public-url", default=DEFAULT_PUBLIC_URL)
    parser.add_argument(
        "--database-url",
        default=None,
        help="finance-sync DATABASE_URL (for delivery freshness)",
    )
    parser.add_argument(
        "--max-stale-hours", type=float, default=DEFAULT_MAX_STALE_HOURS
    )
    parser.add_argument(
        "--json", action="store_true", help="also print JSON status to stderr"
    )
    args = parser.parse_args(argv)

    results = run_all_checks(
        args.public_url, args.max_stale_hours, args.database_url
    )
    sys.stdout.write(render_prometheus(results))
    if args.json:
        sys.stderr.write(
            json.dumps([r.to_dict() for r in results], indent=2) + "\n"
        )
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
