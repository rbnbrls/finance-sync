"""Tests for the finance-sync health monitor (health_monitor.py).

Ported from ~/.hermes/scripts/test_finance_sync_monitor.py into the repo.
The module is env-only: tests set STATE_FILE / GITHUB_TOKEN / COOLIFY_API_TOKEN
via monkeypatch instead of mutating module globals.
"""
# pyright: basic

from __future__ import annotations

import json
import re
import sys
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from finance_sync.monitoring import health_monitor as mod

# Coolify identifiers used by the tests.  The deleted UUID is the literal this
# module used to commit as its default (issue: the monitor silently watched an
# application that no longer exists).
PRODUCTION_UUID = "gavhbmfdg1xiy47ewadkcnfa"
DELETED_UUID = "mdeal4aqq9ycnozn3mg83zix"
TEST_APP_UUID = "tst000000000000000000000"


@pytest.fixture
def monitor_env(tmp_path, monkeypatch):
    """Point the monitor at a temp state file and set a GitHub token."""
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_12345")
    return mod


@pytest.fixture
def resolved_target(monkeypatch):
    """Stub target resolution so main() tests never read the provider."""
    target = mod.MonitorTarget(
        uuid=TEST_APP_UUID,
        name="finance-sync-production",
        health_base_url="https://prod-finance-sync.7rb.nl",
    )
    monkeypatch.setattr(mod, "resolve_target", lambda: target)
    return target


# ═══════════════════════════════════════════════════════════════════
# Tests for issue body builders
# ═══════════════════════════════════════════════════════════════════


class TestBuildCrashIssueBody:
    """Tests for ``build_crash_issue_body``."""

    def test_contains_basic_info(self, monitor_env):
        """Body should include timestamp, HTTP code, status, restart count."""
        timestamp = "2026-07-25T12:00:00+00:00"

        body = mod.build_crash_issue_body(
            timestamp=timestamp,
            app_health=503,
            cf_status="exited",
            restart_count=5,
            restarts_changed=True,
            resources={},
        )

        assert timestamp in body
        assert "503" in body
        assert "exited" in body
        assert "5" in body
        assert "Crash" in body

    def test_contains_dedup_marker(self, monitor_env):
        """Body should include a hidden HTML dedup marker with today's date."""
        from datetime import UTC, datetime

        body = mod.build_crash_issue_body(
            timestamp="2026-07-25T12:00:00+00:00",
            app_health=503,
            cf_status="exited",
            restart_count=5,
            restarts_changed=True,
            resources={},
        )

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert f"<!-- crash-monitor:{today}" in body

    def test_with_resource_data(self, monitor_env):
        """Body should include resource data when resources dict is non-empty."""
        body = mod.build_crash_issue_body(
            timestamp="2026-07-25T12:00:00+00:00",
            app_health=200,
            cf_status="running",
            restart_count=3,
            restarts_changed=False,
            resources={
                "finance-sync-app-1": {
                    "cpu_percent": 92.0,
                    "mem_percent": 85.0,
                    "mem_usage": "512MiB / 1GiB",
                }
            },
        )

        assert "finance-sync-app-1" in body
        assert "92.0%" in body
        assert "85.0%" in body

    def test_no_restart_change_label(self, monitor_env):
        """Should indicate if restart count did not change."""
        body = mod.build_crash_issue_body(
            timestamp="2026-07-25T12:00:00+00:00",
            app_health=503,
            cf_status="running",
            restart_count=5,
            restarts_changed=False,
            resources={},
        )

        assert "restart count" in body.lower()

    def test_restart_changed_label(self, monitor_env):
        """Should indicate restart count increase when restarts_changed."""
        body = mod.build_crash_issue_body(
            timestamp="2026-07-25T12:00:00+00:00",
            app_health=200,
            cf_status="running",
            restart_count=7,
            restarts_changed=True,
            resources={},
        )

        assert "increased" in body.lower() or "new restart" in body.lower()


class TestBuildResourceAlertIssueBody:
    """Tests for ``build_resource_alert_issue_body``."""

    def test_includes_alerts(self, monitor_env):
        """Body should list resource alerts."""
        alerts = [
            "  ⚠ finance-sync-app-1: CPU 92.0% (threshold: 80.0%)",
            "  🚨 finance-sync-worker-1: Memory 95.0% (CRITICAL threshold: 90.0%)",
        ]

        body = mod.build_resource_alert_issue_body(alerts=alerts, resources={})

        for alert in alerts:
            assert alert.strip() in body

    def test_contains_dedup_marker(self, monitor_env):
        """Body should include a hidden HTML dedup marker with today's date."""
        from datetime import UTC, datetime

        body = mod.build_resource_alert_issue_body(
            alerts=["  ⚠ test: CPU 92.0%"], resources={}
        )

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert f"<!-- resource-monitor:{today}" in body

    def test_includes_resource_metrics(self, monitor_env):
        """Body should include container resource metrics when available."""
        resources = {
            "finance-sync-app-1": {
                "cpu_percent": 92.0,
                "mem_percent": 85.0,
                "mem_usage": "512MiB / 1GiB",
            }
        }

        body = mod.build_resource_alert_issue_body(
            alerts=["  ⚠ finance-sync-app-1: CPU 92.0% (threshold: 80.0%)"],
            resources=resources,
        )

        assert "92.0%" in body
        assert "85.0%" in body
        assert "512MiB" in body


# ═══════════════════════════════════════════════════════════════════
# Tests for issue creation + dedup
# ═══════════════════════════════════════════════════════════════════


class TestCreateGitHubIssue:
    """Tests for ``create_github_issue``."""

    def test_success_returns_issue_url(self, monitor_env):
        """A 201 response should return the issue URL."""

        def mock_urlopen(request, **kwargs):
            response = MagicMock()
            response.read.return_value = json.dumps(
                {
                    "html_url": "https://github.com/rbnbrls/finance-sync/issues/42",
                    "number": 42,
                }
            ).encode()
            response.status = 201
            response.__enter__.return_value = response
            return response

        with patch.object(mod, "urlopen", mock_urlopen):
            result = mod.create_github_issue(
                owner="rbnbrls",
                repo="finance-sync",
                title="Test issue",
                body="Test body",
                labels=["bug"],
            )

        assert result is not None
        assert "https://github.com/rbnbrls/finance-sync/issues/42" in result

    def test_http_error_returns_none(self, monitor_env):
        """An HTTP error should return None without raising."""

        from urllib.error import HTTPError

        def mock_urlopen(request, **kwargs):
            raise HTTPError(
                url="https://api.github.com/repos/rbnbrls/finance-sync/issues",
                code=422,
                msg="Validation Failed",
                hdrs=Message(),
                fp=None,
            )

        with patch.object(mod, "urlopen", mock_urlopen):
            result = mod.create_github_issue(
                owner="rbnbrls",
                repo="finance-sync",
                title="Test",
                body="Body",
                labels=["bug"],
            )

        assert result is None

    def test_sends_correct_headers(self, monitor_env):
        """Should send Authorization and Content-Type headers."""

        captured = {}

        def mock_urlopen(request, **kwargs):
            captured["headers"] = dict(request.headers)
            captured["method"] = request.method
            captured["url"] = request.full_url
            response = MagicMock()
            response.read.return_value = json.dumps(
                {
                    "html_url": "https://github.com/rbnbrls/finance-sync/issues/42"
                }
            ).encode()
            response.status = 201
            response.__enter__.return_value = response
            return response

        with patch.object(mod, "urlopen", mock_urlopen):
            mod.create_github_issue(
                owner="rbnbrls",
                repo="finance-sync",
                title="Test",
                body="Body",
                labels=["bug"],
            )

        assert captured["method"] == "POST"
        assert "api.github.com" in captured["url"]
        assert "Bearer" in captured["headers"].get("Authorization", "")

    def test_without_labels(self, monitor_env):
        """Labels field should be omitted when not provided."""

        captured_body = {}

        def mock_urlopen(request, **kwargs):
            captured_body["data"] = request.data
            response = MagicMock()
            response.read.return_value = json.dumps(
                {"html_url": "https://github.com/rbnbrls/finance-sync/issues/1"}
            ).encode()
            response.status = 201
            response.__enter__.return_value = response
            return response

        with patch.object(mod, "urlopen", mock_urlopen):
            mod.create_github_issue(
                owner="rbnbrls",
                repo="finance-sync",
                title="Test",
                body="Body",
            )

        body = json.loads(captured_body["data"])
        assert "labels" not in body

    def test_missing_token(self, monitor_env, monkeypatch):
        """Missing GITHUB_TOKEN should log warning and return None."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        result = mod.create_github_issue(
            owner="rbnbrls",
            repo="finance-sync",
            title="Test",
            body="Body",
        )

        assert result is None

    def test_token_only_from_env(self, monitor_env, monkeypatch):
        """Token must come from env — no ~/.hermes/.env fallback exists."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert mod.get_github_token() is None

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env_999")
        assert mod.get_github_token() == "ghp_from_env_999"


class TestCheckExistingIssue:
    """Tests for ``check_existing_issue``."""

    def test_returns_true_when_open_issue_exists(self, monitor_env):
        """Should return True when an open issue with the marker exists."""

        search_response = {
            "total_count": 1,
            "items": [
                {
                    "number": 42,
                    "title": "Crash detected on finance-sync",
                    "state": "open",
                    "html_url": "https://github.com/rbnbrls/finance-sync/issues/42",
                }
            ],
        }

        def mock_urlopen(request, **kwargs):
            response = MagicMock()
            response.read.return_value = json.dumps(search_response).encode()
            response.status = 200
            response.__enter__.return_value = response
            return response

        with patch.object(mod, "urlopen", mock_urlopen):
            exists = mod.check_existing_issue(
                owner="rbnbrls",
                repo="finance-sync",
                marker="crash-monitor:2026-07-25",
            )

        assert exists is True

    def test_returns_false_when_no_open_issue(self, monitor_env):
        """Should return False when no matching open issue exists."""

        search_response = {"total_count": 0, "items": []}

        def mock_urlopen(request, **kwargs):
            response = MagicMock()
            response.read.return_value = json.dumps(search_response).encode()
            response.status = 200
            response.__enter__.return_value = response
            return response

        with patch.object(mod, "urlopen", mock_urlopen):
            exists = mod.check_existing_issue(
                owner="rbnbrls",
                repo="finance-sync",
                marker="crash-monitor:2026-07-25",
            )

        assert exists is False

    def test_returns_false_on_api_error(self, monitor_env):
        """Should return False on API error (conservative — skip dedup)."""

        from urllib.error import HTTPError

        def mock_urlopen(request, **kwargs):
            raise HTTPError(
                url="https://api.github.com/search/issues",
                code=403,
                msg="Forbidden",
                hdrs=Message(),
                fp=None,
            )

        with patch.object(mod, "urlopen", mock_urlopen):
            exists = mod.check_existing_issue(
                owner="rbnbrls",
                repo="finance-sync",
                marker="crash-monitor:2026-07-25",
            )

        assert exists is False


# ═══════════════════════════════════════════════════════════════════
# Tests for Coolify API check (auth header + state file env)
# ═══════════════════════════════════════════════════════════════════


class TestCheckCoolifyApp:
    """Tests for ``check_coolify_app`` — the fixed auth path."""

    def test_uses_coolify_token_in_header(self, monitor_env, monkeypatch):
        """Auth header must use COOLIFY_API_TOKEN (not GITHUB_TOKEN)."""
        monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify_secret_123")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_appear")

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            result = MagicMock()
            result.stdout = json.dumps(
                {
                    "status": "running",
                    "restart_count": 7,
                    "last_online_at": "2026-08-14T10:00:00Z",
                }
            )
            return result

        with patch.object(mod.subprocess, "run", fake_run):
            info = mod.check_coolify_app(TEST_APP_UUID)

        assert TEST_APP_UUID in captured["cmd"][-3]
        assert captured["cmd"][-2] == "-H"
        assert captured["cmd"][-1] == "Authorization: Bearer coolify_secret_123"
        assert "ghp_should_not_appear" not in " ".join(captured["cmd"])
        assert info["restart_count"] == 7
        assert info["status"] == "running"

    def test_returns_restart_count(self, monitor_env, monkeypatch):
        """Restart count parsed from the Coolify API response."""
        monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify_secret_123")

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = json.dumps(
                {
                    "status": "running",
                    "restart_count": 3,
                    "last_online_at": "never",
                }
            )
            return result

        with patch.object(mod.subprocess, "run", fake_run):
            info = mod.check_coolify_app(TEST_APP_UUID)

        assert info["restart_count"] == 3
        assert info["last_online"] == "never"

    def test_no_token_returns_error_without_crash(
        self, monitor_env, monkeypatch
    ):
        """Missing COOLIFY_API_TOKEN must not NameError — returns error dict."""
        monkeypatch.delenv("COOLIFY_API_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_12345")

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            result = MagicMock()
            result.stdout = "{}"
            return result

        with patch.object(mod.subprocess, "run", fake_run):
            info = mod.check_coolify_app(TEST_APP_UUID)

        # curl still runs, but no Authorization header is attached
        assert captured["cmd"]
        assert all(
            not part.startswith("Authorization") for part in captured["cmd"]
        )
        assert info["restart_count"] == -1


class TestStateFileEnv:
    """Tests for STATE_FILE env handling."""

    def test_state_file_env_honored(self, tmp_path, monkeypatch):
        """STATE_FILE env must control where state is written."""
        custom = tmp_path / "custom" / "nested" / "state.json"
        monkeypatch.setenv("STATE_FILE", str(custom))

        mod.save_state(
            {
                "started_at": None,
                "checks": [],
                "last_restart_count": -1,
                "last_status": None,
            }
        )

        assert custom.exists()
        data = json.loads(custom.read_text())
        assert data["last_restart_count"] == -1

    def test_state_file_default(self, monkeypatch):
        """Without STATE_FILE, the documented default path is used."""
        monkeypatch.delenv("STATE_FILE", raising=False)
        assert (
            mod.get_state_file()
            == "/var/lib/finance-sync/finance-sync-monitor-state.json"
        )


# ═══════════════════════════════════════════════════════════════════
# Tests for issue creation in main flow
# ═══════════════════════════════════════════════════════════════════


class TestMainIntegration:
    """Tests for main() with GitHub issue creation (mocked HTTP)."""

    @pytest.fixture(autouse=True)
    def _stub_target(self, resolved_target):
        """Stub target resolution — these tests mock HTTP, not setup."""
        return resolved_target

    def test_crash_creates_issue_with_bug_label(self, monitor_env):
        """Crash detection should create a GitHub issue with label 'bug'."""

        # Mock state: last_restart_count=0 so an increase to 1 is detected
        mod.load_state = lambda: {
            "started_at": "2026-07-25T11:00:00+00:00",
            "checks": [],
            "last_restart_count": 0,
            "last_status": None,
        }

        # Mock health checks: health fails (non-200) to trigger crash
        mod.check_health = lambda url: 503
        mod.check_coolify_app = lambda _uuid: {
            "status": "running",
            "restart_count": 1,
            "last_online": "2026-07-25T11:59:00Z",
        }
        mod.check_container_resources = dict

        # Mock check_existing_issue to return False (no duplicate)
        mod.check_existing_issue = lambda owner, repo, marker: False

        # Track issue creation
        created_issues = []

        def fake_create_issue(*args, **kwargs):
            created_issues.append((args, kwargs))
            return "https://github.com/rbnbrls/finance-sync/issues/1"

        mod.create_github_issue = fake_create_issue

        # Run main
        with patch.object(sys, "exit"):
            mod.main()

        # Should have created exactly one issue with bug label
        assert len(created_issues) == 1, (
            f"Expected 1 issue, got {len(created_issues)}. "
            f"created_issues={created_issues}"
        )
        _, kwargs = created_issues[0]
        assert kwargs.get("labels") == ["bug"]

    def test_crash_with_duplicate_skips_issue(self, monitor_env):
        """Duplicate crash event should NOT create a new issue."""

        mod.load_state = lambda: {
            "started_at": "2026-07-25T11:00:00+00:00",
            "checks": [],
            "last_restart_count": 0,
            "last_status": None,
        }

        mod.check_health = lambda url: 503
        mod.check_coolify_app = lambda _uuid: {
            "status": "running",
            "restart_count": 1,
            "last_online": "2026-07-25T11:59:00Z",
        }
        mod.check_container_resources = dict

        # Simulate that an issue for today already exists
        mod.check_existing_issue = lambda owner, repo, marker: True

        created_issues = []

        def fake_create_issue(*args, **kwargs):
            created_issues.append((args, kwargs))
            return "https://github.com/rbnbrls/finance-sync/issues/1"

        mod.create_github_issue = fake_create_issue

        with patch.object(sys, "exit"):
            mod.main()

        # Should NOT create a duplicate issue
        assert len(created_issues) == 0

    def test_resource_alert_creates_issue_with_enhancement_label(
        self, monitor_env
    ):
        """Resource threshold exceeded should create an issue with 'enhancement'."""

        mod.load_state = lambda: {
            "started_at": "2026-07-25T11:00:00+00:00",
            "checks": [],
            "last_restart_count": 0,
            "last_status": "running",
        }

        mod.check_health = lambda url: 200
        mod.check_coolify_app = lambda _uuid: {
            "status": "running",
            "restart_count": 0,
            "last_online": "2026-07-25T11:59:00Z",
        }

        # Simulate high CPU
        mod.check_container_resources = lambda: {
            "finance-sync-app-1": {
                "cpu_percent": 92.0,
                "mem_percent": 45.0,
                "mem_usage": "256MiB / 1GiB",
            }
        }

        mod.check_existing_issue = lambda owner, repo, marker: False

        created_issues = []

        def fake_create_issue(*args, **kwargs):
            created_issues.append((args, kwargs))
            return "https://github.com/rbnbrls/finance-sync/issues/1"

        mod.create_github_issue = fake_create_issue

        with patch.object(sys, "exit"):
            mod.main()

        assert len(created_issues) == 1
        _, kwargs = created_issues[0]
        assert kwargs.get("labels") == ["enhancement"]

    def test_resource_alert_with_duplicate_skips(self, monitor_env):
        """Duplicate resource alert should NOT create a new issue."""

        mod.load_state = lambda: {
            "started_at": "2026-07-25T11:00:00+00:00",
            "checks": [],
            "last_restart_count": 0,
            "last_status": "running",
        }

        mod.check_health = lambda url: 200
        mod.check_coolify_app = lambda _uuid: {
            "status": "running",
            "restart_count": 0,
            "last_online": "2026-07-25T11:59:00Z",
        }
        mod.check_container_resources = lambda: {
            "finance-sync-app-1": {
                "cpu_percent": 92.0,
                "mem_percent": 45.0,
                "mem_usage": "256MiB / 1GiB",
            }
        }

        mod.check_existing_issue = lambda owner, repo, marker: True

        created_issues = []

        def fake_create_issue(*args, **kwargs):
            created_issues.append((args, kwargs))
            return "https://github.com/rbnbrls/finance-sync/issues/1"

        mod.create_github_issue = fake_create_issue

        with patch.object(sys, "exit"):
            mod.main()

        assert len(created_issues) == 0

    def test_healthy_no_alerts_no_issues_created(self, monitor_env):
        """When healthy with no alerts, no issues should be created."""

        mod.load_state = lambda: {
            "started_at": "2026-07-25T11:00:00+00:00",
            "checks": [],
            "last_restart_count": 0,
            "last_status": "running",
        }

        mod.check_health = lambda url: 200
        mod.check_coolify_app = lambda _uuid: {
            "status": "running",
            "restart_count": 0,
            "last_online": "2026-07-25T11:59:00Z",
        }
        mod.check_container_resources = dict

        created_issues = []

        def fake_create_issue(*args, **kwargs):
            created_issues.append((args, kwargs))
            return "https://github.com/rbnbrls/finance-sync/issues/1"

        mod.create_github_issue = fake_create_issue

        with patch.object(sys, "exit"):
            mod.main()

        assert len(created_issues) == 0

    def test_exit_code_0_on_success(self, monitor_env):
        """Exit code should be 0 when monitoring succeeds (even with alerts)."""

        mod.load_state = lambda: {
            "started_at": "2026-07-25T11:00:00+00:00",
            "checks": [],
            "last_restart_count": 0,
            "last_status": "running",
        }

        mod.check_health = lambda url: 200
        mod.check_coolify_app = lambda _uuid: {
            "status": "running",
            "restart_count": 0,
            "last_online": "2026-07-25T11:59:00Z",
        }
        mod.check_container_resources = lambda: {
            "finance-sync-app-1": {
                "cpu_percent": 92.0,
                "mem_percent": 45.0,
                "mem_usage": "256MiB / 1GiB",
            }
        }
        mod.check_existing_issue = lambda owner, repo, marker: (
            True
        )  # skip dedup

        with patch.object(sys, "exit") as mock_exit:
            mod.main()

        assert mock_exit.call_args[0][0] == 0

    def test_exit_code_1_when_issue_creation_fails(self, monitor_env):
        """Exit code should be 1 when GitHub issue creation fails."""

        mod.load_state = lambda: {
            "started_at": "2026-07-25T11:00:00+00:00",
            "checks": [],
            "last_restart_count": 0,
            "last_status": "running",
        }

        mod.check_health = lambda url: 200
        mod.check_coolify_app = lambda _uuid: {
            "status": "running",
            "restart_count": 0,
            "last_online": "2026-07-25T11:59:00Z",
        }
        mod.check_container_resources = lambda: {
            "finance-sync-app-1": {
                "cpu_percent": 92.0,
                "mem_percent": 45.0,
                "mem_usage": "256MiB / 1GiB",
            }
        }
        mod.check_existing_issue = lambda owner, repo, marker: False

        mod.create_github_issue = lambda *a, **k: None  # creation fails

        exits: list[int] = []

        def fake_exit(code: int = 0) -> None:
            exits.append(code)
            raise SystemExit(code)

        with (
            patch.object(sys, "exit", side_effect=fake_exit),
            pytest.raises(SystemExit),
        ):
            mod.main()

        # First (real) exit attempt must be 1 — the mocked sys.exit never
        # raises, so without the side effect execution would fall through to
        # the trailing sys.exit(0) and mask the failure path.
        assert exits == [1]


# ═══════════════════════════════════════════════════════════════════
# Tests for target resolution (name → UUID + served route, fail closed)
# ═══════════════════════════════════════════════════════════════════

SERVED_PRODUCTION_ROUTE = "https://prod-finance-sync.7rb.nl"


def _write_applications(tmp_path, applications) -> str:
    """Write an ``/applications`` payload and return the fixture path."""
    path = tmp_path / "applications.json"
    path.write_text(json.dumps(applications), encoding="utf-8")
    return str(path)


def _provider_applications() -> list[dict]:
    """The finance-sync applications the live provider returns."""
    return [
        {
            "name": "finance-sync-development",
            "uuid": "m3jotzz6d1bj0l7ca4v2lmiz",
            "fqdn": "https://finance-sync.7rb.nl",
        },
        {
            "name": "finance-sync-production",
            "uuid": PRODUCTION_UUID,
            "fqdn": f"https://{PRODUCTION_UUID}.7rb.nl",
            # Coolify returns this field as a JSON *string*, and the serving
            # Compose service for this app is `app`, not `web`.
            "docker_compose_domains": json.dumps(
                {"app": {"domain": f"{SERVED_PRODUCTION_ROUTE}/"}}
            ),
        },
        {
            "name": "financesync-test",
            "uuid": "e1iq0lk2wmrdm3dadghhmtlw",
            "fqdn": "https://test-finance-sync.7rb.nl",
        },
    ]


@pytest.fixture
def provider_fixture(tmp_path, monkeypatch):
    """Offline provider read with every target override cleared."""
    applications = _provider_applications()
    monkeypatch.setenv(
        "COOLIFY_APPLICATIONS_FILE", _write_applications(tmp_path, applications)
    )
    for name in (
        "COOLIFY_APP_UUID",
        "COOLIFY_APP_NAME",
        "MONITOR_HEALTH_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return applications


class TestResolveTarget:
    """Target resolution: never a committed UUID, typed failure otherwise."""

    def test_module_has_no_application_uuid_literal(self):
        """No Coolify UUID may be committed in the monitor module."""
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert re.search(r"[a-z0-9]{24}", source) is None
        assert not hasattr(mod, "DEFAULT_APP_UUID")
        assert mod.DEFAULT_APP_NAME == "finance-sync-production"

    def test_default_name(self, provider_fixture):
        """The monitored name comes from COOLIFY_APP_NAME, default production."""
        assert mod.get_app_name() == "finance-sync-production"

    def test_resolves_name_to_uuid_and_served_route(self, provider_fixture):
        """The default target resolves by name onto the route Coolify serves."""
        target = mod.resolve_target()
        assert (target.uuid, target.name) == (
            PRODUCTION_UUID,
            "finance-sync-production",
        )
        # Never the generated <uuid>.7rb.nl host: a Compose app serves its
        # compose-service domain instead.
        assert target.health_base_url == SERVED_PRODUCTION_ROUTE
        assert PRODUCTION_UUID not in target.health_base_url
        assert mod.get_app_uuid() == PRODUCTION_UUID
        assert mod.get_health_base_url() == SERVED_PRODUCTION_ROUTE

    def test_name_env_override(self, provider_fixture, monkeypatch):
        """COOLIFY_APP_NAME selects another application by exact name."""
        monkeypatch.setenv("COOLIFY_APP_NAME", "financesync-test")
        target = mod.resolve_target()
        assert target.uuid == "e1iq0lk2wmrdm3dadghhmtlw"
        assert target.name == "financesync-test"
        assert target.health_base_url == "https://test-finance-sync.7rb.nl"

    def test_explicit_override_with_pinned_url_skips_provider(
        self, monkeypatch
    ):
        """A pinned UUID + URL pair needs no provider read at all."""
        monkeypatch.setenv("COOLIFY_APP_UUID", TEST_APP_UUID)
        monkeypatch.setenv(
            "MONITOR_HEALTH_BASE_URL", f"{SERVED_PRODUCTION_ROUTE}/"
        )
        monkeypatch.delenv("COOLIFY_APPLICATIONS_FILE", raising=False)

        def _boom():
            message = "provider read not expected"
            raise AssertionError(message)

        monkeypatch.setattr(mod, "fetch_applications", _boom)
        target = mod.resolve_target()
        assert target.uuid == TEST_APP_UUID
        assert target.health_base_url == SERVED_PRODUCTION_ROUTE

    def test_uuid_override_resolves_route_from_provider(
        self, provider_fixture, monkeypatch
    ):
        """A UUID override still learns the served route from the provider."""
        monkeypatch.setenv("COOLIFY_APP_UUID", PRODUCTION_UUID)
        target = mod.resolve_target()
        assert target.uuid == PRODUCTION_UUID
        assert target.health_base_url == SERVED_PRODUCTION_ROUTE

    def test_deleted_uuid_override_fails_typed(
        self, provider_fixture, monkeypatch
    ):
        """The UUID this monitor used to default to no longer exists."""
        monkeypatch.setenv("COOLIFY_APP_UUID", DELETED_UUID)
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.resolve_target()
        assert exc.value.reason == (
            f"coolify_application_uuid_not_found:{DELETED_UUID}"
        )

    def test_missing_name_fails_typed(self, provider_fixture, monkeypatch):
        """A name the provider does not have is a typed failure."""
        monkeypatch.setenv("COOLIFY_APP_NAME", "finance-sync-staging")
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.resolve_target()
        assert exc.value.reason == (
            "coolify_application_not_found:finance-sync-staging"
        )

    def test_ambiguous_name_fails_typed(self, tmp_path, monkeypatch):
        """Two applications with the same name are a typed failure."""
        monkeypatch.setenv(
            "COOLIFY_APPLICATIONS_FILE",
            _write_applications(
                tmp_path,
                [
                    {
                        "name": "finance-sync-production",
                        "uuid": "bbbbbbbbbbbbbbbbbbbbbbbb",
                        "fqdn": "b.example.test",
                    },
                    {
                        "name": "finance-sync-production",
                        "uuid": "aaaaaaaaaaaaaaaaaaaaaaaa",
                        "fqdn": "a.example.test",
                    },
                ],
            ),
        )
        monkeypatch.delenv("COOLIFY_APP_UUID", raising=False)
        monkeypatch.delenv("MONITOR_HEALTH_BASE_URL", raising=False)
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.resolve_target()
        assert exc.value.reason == (
            "coolify_application_ambiguous:finance-sync-production:"
            "aaaaaaaaaaaaaaaaaaaaaaaa,bbbbbbbbbbbbbbbbbbbbbbbb"
        )

    def test_uuidless_application_fails_typed(self, tmp_path, monkeypatch):
        """An application entry without a UUID is a typed failure."""
        monkeypatch.setenv(
            "COOLIFY_APPLICATIONS_FILE",
            _write_applications(
                tmp_path,
                [{"name": "finance-sync-production", "fqdn": "x.example.test"}],
            ),
        )
        monkeypatch.delenv("COOLIFY_APP_UUID", raising=False)
        monkeypatch.delenv("MONITOR_HEALTH_BASE_URL", raising=False)
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.resolve_target()
        assert exc.value.reason == (
            "coolify_application_uuid_missing:finance-sync-production"
        )

    def test_route_unresolved_fails_typed(self, tmp_path, monkeypatch):
        """An application with no served route is a typed failure."""
        monkeypatch.setenv(
            "COOLIFY_APPLICATIONS_FILE",
            _write_applications(
                tmp_path,
                [
                    {
                        "name": "finance-sync-production",
                        "uuid": PRODUCTION_UUID,
                        "docker_compose_domains": {},
                    }
                ],
            ),
        )
        monkeypatch.delenv("COOLIFY_APP_UUID", raising=False)
        monkeypatch.delenv("MONITOR_HEALTH_BASE_URL", raising=False)
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.resolve_target()
        assert exc.value.reason == (
            "coolify_domain_unresolved:finance-sync-production"
        )

    @pytest.mark.parametrize(
        ("application", "expected"),
        [
            pytest.param(
                {
                    "docker_compose_domains": {
                        "app": {"domain": "https://app.example.test"},
                        "web": {"domain": "web.example.test/some/path"},
                    }
                },
                "web.example.test",
                id="web-wins",
            ),
            pytest.param(
                {
                    "docker_compose_domains": json.dumps(
                        {"app": {"domain": f"{SERVED_PRODUCTION_ROUTE}/"}}
                    ),
                    "fqdn": f"https://{PRODUCTION_UUID}.7rb.nl",
                },
                "prod-finance-sync.7rb.nl",
                id="single-non-web-service",
            ),
            pytest.param(
                {
                    "docker_compose_domains": {
                        "api": {"domain": "API.Example.Test."},
                    },
                    "fqdn": f"https://{PRODUCTION_UUID}.7rb.nl",
                },
                "api.example.test",
                id="normalised",
            ),
            pytest.param(
                {
                    "docker_compose_domains": {
                        "a": {"domain": "a.example.test"},
                        "b": {"domain": "b.example.test"},
                    },
                    "fqdn": "https://fallback.example.test",
                },
                "fallback.example.test",
                id="conflicting-services-use-fqdn",
            ),
            pytest.param(
                {"fqdn": "https://only-fqdn.example.test"},
                "only-fqdn.example.test",
                id="fqdn-only",
            ),
            pytest.param(
                {
                    "docker_compose_domains": "not-json",
                    "fqdn": "f.example.test",
                },
                "f.example.test",
                id="malformed-domains-ignored",
            ),
            pytest.param({"fqdn": None}, "", id="nothing-served"),
        ],
    )
    def test_served_host_derivation(self, application, expected):
        """The serving route wins over the generated UUID host."""
        assert mod.served_host(application) == expected


class TestFetchApplications:
    """Reading the provider (or its offline fixture) with typed failures."""

    def test_curl_uses_token_and_applications_url(self, monkeypatch):
        """The provider read is an authenticated GET /applications."""
        monkeypatch.delenv("COOLIFY_APPLICATIONS_FILE", raising=False)
        monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify_test_token")
        monkeypatch.setenv(
            "COOLIFY_API_URL", "https://coolify.example.test/api/v1"
        )
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(_provider_applications())
            )

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        applications = mod.fetch_applications()

        assert [app["name"] for app in applications] == [
            "finance-sync-development",
            "finance-sync-production",
            "financesync-test",
        ]
        assert captured["cmd"][-1] == (
            "https://coolify.example.test/api/v1/applications"
        )
        assert "Authorization: Bearer coolify_test_token" in captured["cmd"]

    def test_wrapped_payload_is_accepted(self, tmp_path, monkeypatch):
        """A wrapped API shape reports the same applications, not an error."""
        monkeypatch.setenv(
            "COOLIFY_APPLICATIONS_FILE",
            _write_applications(
                tmp_path, {"applications": _provider_applications()}
            ),
        )
        assert len(mod.fetch_applications()) == 3

    def test_missing_token_fails_typed(self, monkeypatch):
        """Without a token — and no fixture — the provider cannot be read."""
        monkeypatch.delenv("COOLIFY_APPLICATIONS_FILE", raising=False)
        monkeypatch.delenv("COOLIFY_API_TOKEN", raising=False)
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.fetch_applications()
        assert exc.value.reason == "coolify_api_token_missing"

    def test_unreachable_provider_fails_typed(self, monkeypatch):
        """A non-zero curl exit is a typed unreachable reason."""
        monkeypatch.delenv("COOLIFY_APPLICATIONS_FILE", raising=False)
        monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify_test_token")
        monkeypatch.setenv(
            "COOLIFY_API_URL", "https://coolify.example.test/api/v1"
        )
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda cmd, **kwargs: SimpleNamespace(returncode=6, stdout=""),
        )
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.fetch_applications()
        assert exc.value.reason == (
            "coolify_api_unreachable:"
            "https://coolify.example.test/api/v1/applications"
        )

    def test_curl_oserror_fails_typed(self, monkeypatch):
        """An OSError from curl is a typed unreachable reason."""
        monkeypatch.delenv("COOLIFY_APPLICATIONS_FILE", raising=False)
        monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify_test_token")

        def _boom(cmd, **kwargs):
            message = "curl not found"
            raise OSError(message)

        monkeypatch.setattr(mod.subprocess, "run", _boom)
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.fetch_applications()
        assert exc.value.reason.startswith("coolify_api_unreachable:")

    def test_unparsable_payload_fails_typed(self, tmp_path, monkeypatch):
        """A payload that is not a list of applications is a typed failure."""
        path = tmp_path / "applications.json"
        path.write_text("not json", encoding="utf-8")
        monkeypatch.setenv("COOLIFY_APPLICATIONS_FILE", str(path))
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.fetch_applications()
        assert exc.value.reason == "coolify_applications_unparsable"

    def test_missing_fixture_fails_typed(self, tmp_path, monkeypatch):
        """A missing offline fixture is a typed failure, not a traceback."""
        missing = tmp_path / "nope.json"
        monkeypatch.setenv("COOLIFY_APPLICATIONS_FILE", str(missing))
        with pytest.raises(mod.MonitorConfigError) as exc:
            mod.fetch_applications()
        assert exc.value.reason == (
            f"coolify_applications_fixture_missing:{missing}"
        )


class TestMainTargetResolution:
    """main() fails closed when the target cannot be resolved."""

    @staticmethod
    def _clear_target_env(monkeypatch) -> None:
        for name in (
            "COOLIFY_APP_UUID",
            "COOLIFY_APP_NAME",
            "MONITOR_HEALTH_BASE_URL",
            "COOLIFY_APPLICATIONS_FILE",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_exits_2_when_name_is_missing(
        self, monitor_env, tmp_path, monkeypatch, capsys
    ):
        """An unresolvable name exits 2 with a typed reason and no probe."""
        self._clear_target_env(monkeypatch)
        monkeypatch.setenv(
            "COOLIFY_APPLICATIONS_FILE", _write_applications(tmp_path, [])
        )

        with pytest.raises(SystemExit) as exc:
            mod.main()

        assert exc.value.code == 2
        assert (
            "error:coolify_application_not_found:finance-sync-production"
            in capsys.readouterr().err
        )
        # Fail closed: nothing probed, nothing recorded.
        assert not Path(mod.get_state_file()).exists()

    def test_exits_2_when_token_is_missing(
        self, monitor_env, monkeypatch, capsys
    ):
        """No token and no fixture means no resolvable target."""
        self._clear_target_env(monkeypatch)
        monkeypatch.delenv("COOLIFY_API_TOKEN", raising=False)

        with pytest.raises(SystemExit) as exc:
            mod.main()

        assert exc.value.code == 2
        assert "error:coolify_api_token_missing" in capsys.readouterr().err

    def test_probes_the_resolved_route(
        self, monitor_env, resolved_target, monkeypatch
    ):
        """Both probes use the resolved route and the resolved UUID."""
        probed: list[str] = []
        checked: list[str] = []

        monkeypatch.setattr(
            mod,
            "load_state",
            lambda: {
                "started_at": "2026-07-25T11:00:00+00:00",
                "checks": [],
                "last_restart_count": 0,
                "last_status": "running",
            },
        )
        monkeypatch.setattr(
            mod, "check_health", lambda url: probed.append(url) or 200
        )
        monkeypatch.setattr(
            mod,
            "check_coolify_app",
            lambda app_uuid: (
                checked.append(app_uuid)
                or {
                    "status": "running",
                    "restart_count": 0,
                    "last_online": "2026-07-25T11:59:00Z",
                }
            ),
        )
        monkeypatch.setattr(mod, "check_container_resources", dict)
        monkeypatch.setattr(mod, "save_state", lambda state: None)

        with patch.object(sys, "exit"):
            mod.main()

        assert probed == [
            f"{resolved_target.health_base_url}/health/live",
            f"{resolved_target.health_base_url}/health/ready",
        ]
        assert checked == [resolved_target.uuid]
