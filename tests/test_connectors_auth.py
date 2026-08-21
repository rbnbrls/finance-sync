"""Authorization tests for the connectors API surface (issue #268).

Regression coverage for the connectors page failing for role ``user`` with
"Required one of roles ('admin',), got 'user'": the DEGIRO import endpoints
under ``/connectors/degiro-pension/imports`` were guarded by the hard-coded
``require_role("admin")`` dependency instead of the application's permission
system.  They now use ``require_permission("connectors", read|write)``, which
the ``user`` role already carries while anonymous principals are still
rejected with 401.
"""

# pyright: basic

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import RedisDsn, SecretStr

from finance_sync.api.deps.auth import (
    APIKeyAuthResult,
    AuthContext,
    get_auth_context,
)
from finance_sync.app import create_app
from finance_sync.config.environments import Environment
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from fastapi import FastAPI

_TENANT = "tenant-1"
_CONFIGS_URL = "/api/v1/connectors/configs"
_IMPORTS_URL = "/api/v1/connectors/degiro-pension/imports"


def _user_ctx(role: str) -> AuthContext:
    """JWT-style principal carrying only a role (like a decoded token)."""
    user = MagicMock()
    user.role = role
    user.tenant_id = _TENANT
    user.id = "user-1"
    return AuthContext(user=user)


def _api_key_ctx(permissions: str) -> AuthContext:
    """API key principal carrying an explicit permission string."""
    return AuthContext(
        api_key_result=APIKeyAuthResult(
            permissions=permissions,
            tenant_id=_TENANT,
        )
    )


def _db_mock() -> AsyncMock:
    """Session stub: no connector configs, no import runs, no credentials."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.return_value = result
    return db


def _make_app(
    auth_ctx: AuthContext | None,
    staging_dir: Path,
) -> FastAPI:
    app = create_app(
        Settings(
            environment=Environment.PRODUCTION,
            database_url=None,
            redis_url=RedisDsn("redis://localhost:6379/0"),
            secret_key=SecretStr("test-production-secret-key-1234"),
            master_encryption_key=SecretStr("a1b2c3d4" * 8),
            cors_origins=["https://example.test"],
            degiro_import_staging_directory=staging_dir,
        )
    )
    if auth_ctx is not None:
        app.dependency_overrides[get_auth_context] = lambda ctx=auth_ctx: ctx
    app.dependency_overrides[get_db] = _db_mock
    return app


@pytest.fixture
def staging_dir(tmp_path: Path) -> Generator[Path, None, None]:
    yield tmp_path / "degiro-imports"


@contextmanager
def _client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ── GET /connectors/configs ────────────────────────────────────────────


def test_connectors_configs_requires_auth(staging_dir: Path) -> None:
    with _client(_make_app(None, staging_dir)) as client:
        resp = client.get(_CONFIGS_URL)
        assert resp.status_code == 401  # no Bearer token / API key


def test_connectors_configs_allows_user_role(staging_dir: Path) -> None:
    with _client(_make_app(_user_ctx("user"), staging_dir)) as client:
        resp = client.get(_CONFIGS_URL)
        assert resp.status_code == 200
        assert resp.json() == []


def test_connectors_configs_allows_api_key_with_permission(
    staging_dir: Path,
) -> None:
    with _client(
        _make_app(_api_key_ctx("connectors:read"), staging_dir)
    ) as client:
        resp = client.get(_CONFIGS_URL)
        assert resp.status_code == 200


def test_connectors_configs_rejects_readonly(staging_dir: Path) -> None:
    with _client(_make_app(_user_ctx("readonly"), staging_dir)) as client:
        resp = client.get(_CONFIGS_URL)
        assert resp.status_code == 403  # readonly lacks connectors:read


# ── GET /connectors/degiro-pension/imports ─────────────────────────────


def test_degiro_import_runs_requires_auth(staging_dir: Path) -> None:
    with _client(_make_app(None, staging_dir)) as client:
        resp = client.get(_IMPORTS_URL)
        assert resp.status_code == 401


def test_degiro_import_runs_allows_user_role(staging_dir: Path) -> None:
    # The exact regression from issue #268: previously 403 with
    # "Required one of roles ('admin',), got 'user'".
    with _client(_make_app(_user_ctx("user"), staging_dir)) as client:
        resp = client.get(_IMPORTS_URL)
        assert resp.status_code == 200
        assert resp.json() == []


def test_degiro_import_runs_allows_admin(staging_dir: Path) -> None:
    with _client(_make_app(_user_ctx("admin"), staging_dir)) as client:
        resp = client.get(_IMPORTS_URL)
        assert resp.status_code == 200


def test_degiro_import_runs_rejects_viewer(staging_dir: Path) -> None:
    with _client(_make_app(_user_ctx("viewer"), staging_dir)) as client:
        resp = client.get(_IMPORTS_URL)
        assert resp.status_code == 403  # viewer lacks connectors:read


# ── POST /connectors/degiro-pension/imports/preview ────────────────────


def test_degiro_preview_requires_auth(staging_dir: Path) -> None:
    with _client(_make_app(None, staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/preview")
        assert resp.status_code == 401


def test_degiro_preview_rejects_readonly_without_write(
    staging_dir: Path,
) -> None:
    # 403 fires before any multipart body parsing, so no form data needed.
    with _client(_make_app(_user_ctx("readonly"), staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/preview")
        assert resp.status_code == 403  # readonly lacks connectors:write


# ── POST /connectors/degiro-pension/imports/{run_id}/confirm ───────────


def test_degiro_confirm_requires_auth(staging_dir: Path) -> None:
    with _client(_make_app(None, staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/run-1/confirm", json={})
        assert resp.status_code == 401


def test_degiro_confirm_reachable_by_user_role(staging_dir: Path) -> None:
    # Same as preview: user passes the gate, then the run lookup 404s.
    with _client(_make_app(_user_ctx("user"), staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/run-1/confirm", json={})
        assert resp.status_code == 404


def test_degiro_confirm_force_reimport_is_admin_only(staging_dir: Path) -> None:
    ctx_404 = {"force_reimport": True}
    with _client(_make_app(_user_ctx("user"), staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/run-1/confirm", json=ctx_404)
        assert resp.status_code == 403  # admin-only override flag

    with _client(_make_app(_user_ctx("admin"), staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/run-1/confirm", json=ctx_404)
        assert resp.status_code == 404  # admin passes the flag gate


def test_degiro_confirm_rejects_readonly(staging_dir: Path) -> None:
    with _client(_make_app(_user_ctx("readonly"), staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/run-1/confirm", json={})
        assert resp.status_code == 403  # readonly lacks connectors:write


# ── POST /connectors/degiro-pension/imports/{run_id}/retry ─────────────


def test_degiro_retry_requires_write_permission(staging_dir: Path) -> None:
    with _client(_make_app(_user_ctx("readonly"), staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/run-1/retry")
        assert resp.status_code == 403


def test_degiro_retry_requires_auth(staging_dir: Path) -> None:
    with _client(_make_app(None, staging_dir)) as client:
        resp = client.post(f"{_IMPORTS_URL}/run-1/retry")
        assert resp.status_code == 401
