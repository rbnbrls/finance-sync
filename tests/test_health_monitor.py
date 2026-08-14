"""Tests for the finance-sync health monitor (health_monitor.py).

Ported from ~/.hermes/scripts/test_finance_sync_monitor.py into the repo.
The module is env-only: tests set STATE_FILE / GITHUB_TOKEN / COOLIFY_API_TOKEN
via monkeypatch instead of mutating module globals.
"""
# pyright: basic

from __future__ import annotations

import json
import sys
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

from finance_sync.monitoring import health_monitor as mod


@pytest.fixture
def monitor_env(tmp_path, monkeypatch):
    """Point the monitor at a temp state file and set a GitHub token."""
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_12345")
    return mod


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
            info = mod.check_coolify_app()

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
            info = mod.check_coolify_app()

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
            info = mod.check_coolify_app()

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
        mod.check_coolify_app = lambda: {
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
        mod.check_coolify_app = lambda: {
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
        mod.check_coolify_app = lambda: {
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
        mod.check_coolify_app = lambda: {
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
        mod.check_coolify_app = lambda: {
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
        mod.check_coolify_app = lambda: {
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
        mod.check_coolify_app = lambda: {
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
