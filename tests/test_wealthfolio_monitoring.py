"""Tests for the Wealthfolio multi-device access monitor (backlog AC).

Covers HTTPS reachability, certificate expiry, Wealthfolio health and the
export freshness checks, plus the Prometheus rendering guarantee: no
financial values or secrets ever appear in metric labels.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from finance_sync.monitoring.wealthfolio_monitor import (
    check_cert_expiry,
    check_export_freshness,
    check_https_reachable,
    check_wealthfolio_health,
    normalize_pg_url,
    render_prometheus,
    run_all_checks,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeHandler(BaseHTTPRequestHandler):
    """Serves a minimal Wealthfolio-like endpoint set."""

    def do_GET(self) -> None:
        if self.path.startswith("/api/v1/auth/status"):
            body = b'{"requiresPassword": true, "oidcEnabled": false}'
            self.send_response(200)
        elif self.path == "/":
            body = b"<!doctype html><html><body>Wealthfolio PWA</body></html>"
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def fake_http_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_https_reachable_ok(fake_http_server: str) -> None:
    result = check_https_reachable(fake_http_server)
    assert result.ok is True
    assert result.name == "https_reachable"


def test_https_reachable_fails_on_missing_host() -> None:
    result = check_https_reachable("http://127.0.0.1:1")
    assert result.ok is False


def test_wealthfolio_health_ok(fake_http_server: str) -> None:
    result = check_wealthfolio_health(fake_http_server)
    assert result.ok is True
    assert "requiresPassword=True" in result.detail


def test_wealthfolio_health_fails_when_password_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    class _Resp:
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"requiresPassword": false, "oidcEnabled": false}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    result = check_wealthfolio_health("https://example.test")
    assert result.ok is False


def test_cert_expiry_parses_notafter(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    class _CertResp:
        def __enter__(self) -> _CertResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def getpeercert(self) -> dict[str, Any]:
            far = (datetime.now(UTC) + timedelta(days=90)).strftime(
                "%b %d %H:%M:%S %Y GMT"
            )
            return {"notAfter": far}

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _CertResp())
    result = check_cert_expiry("https://wealthfolio.example.test", warn_days=21)
    assert result.ok is True
    assert result.value is not None and result.value > 21


def test_cert_expiry_fails_when_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    class _CertResp:
        def __enter__(self) -> _CertResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def getpeercert(self) -> dict[str, Any]:
            soon = (datetime.now(UTC) + timedelta(days=5)).strftime(
                "%b %d %H:%M:%S %Y GMT"
            )
            return {"notAfter": soon}

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _CertResp())
    result = check_cert_expiry("https://wealthfolio.example.test", warn_days=21)
    assert result.ok is False


def test_cert_expiry_handles_iso_notafter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISO-8601 notAfter (emitted by some TLS stacks) must not crash the check."""
    import urllib.request

    class _CertResp:
        def __enter__(self) -> _CertResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def getpeercert(self) -> dict[str, Any]:
            far = (datetime.now(UTC) + timedelta(days=90)).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            return {"notAfter": far}

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _CertResp())
    result = check_cert_expiry("https://wealthfolio.example.test", warn_days=21)
    assert result.ok is True
    assert result.value is not None and result.value > 21


def test_cert_expiry_fails_cleanly_on_unparseable_notafter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed notAfter must fail the check, not crash the whole monitor."""
    import urllib.request

    class _CertResp:
        def __enter__(self) -> _CertResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def getpeercert(self) -> dict[str, Any]:
            return {"notAfter": "not-a-real-date"}

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _CertResp())
    result = check_cert_expiry("https://wealthfolio.example.test", warn_days=21)
    assert result.ok is False
    assert "notAfter" in result.detail


def test_export_freshness_ok_when_recent() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    deliveries = [(now - timedelta(minutes=30), "acct-1")]
    result = check_export_freshness(deliveries, max_stale_hours=24, now=now)
    assert result.ok is True
    assert result.value is not None and result.value < 1


def test_export_freshness_fails_when_stale() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    deliveries = [(now - timedelta(hours=50), "acct-1")]
    result = check_export_freshness(deliveries, max_stale_hours=24, now=now)
    assert result.ok is False


def test_export_freshness_fails_when_no_data() -> None:
    result = check_export_freshness([], max_stale_hours=24)
    assert result.ok is False


def test_prometheus_labels_contain_no_financial_values() -> None:
    results = [
        check_export_freshness(
            [(datetime(2026, 8, 17, 12, 0, tzinfo=UTC), "acct-1")],
            now=datetime(2026, 8, 17, 12, 30, tzinfo=UTC),
        )
    ]
    text = render_prometheus(results)
    assert 'check="export_freshness"' in text
    # No account ids, no secrets, no amounts in the label set.
    assert "acct-1" not in text
    assert "secret" not in text.lower()


def test_normalize_pg_url() -> None:
    assert (
        normalize_pg_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
    assert (
        normalize_pg_url("postgresql+psycopg://u:p@h/db")
        == "postgresql://u:p@h/db"
    )
    assert normalize_pg_url("postgresql://u@h/db") == "postgresql://u@h/db"


def test_run_all_checks_fail_closed_without_db() -> None:
    results = run_all_checks("http://127.0.0.1:1", 24, database_url=None)
    names = {r.name for r in results}
    assert names == {
        "https_reachable",
        "cert_expiry",
        "wealthfolio_health",
        "export_freshness",
    }
    by_name = {r.name: r for r in results}
    assert by_name["export_freshness"].ok is False  # fail closed
