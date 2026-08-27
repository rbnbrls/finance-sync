"""API tests for the tenant-scoped sync-schedule endpoints.

Covers the acceptance criteria at the HTTP layer: list/get/preview/
update/reset/disable/enable, permission gating (sync:read vs
sync:write), tenant isolation (foreign ids behave like missing ids),
optimistic-lock 409s, consistent 422 validation and audit records.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Make JSONB work with SQLite (same pattern as test_reconciliation_integration).
if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[assignment]

# Also make the Uuid bind processor accept strings (not just UUID objects)
import uuid as _uuid_mod

from sqlalchemy import types as _sa_types

_uuid_bind_orig = _sa_types.Uuid.bind_processor


def _uuid_bind_patched(self, dialect):
    proc = _uuid_bind_orig(self, dialect)
    if proc is None or not self.as_uuid:
        return proc

    def _patched(value):
        if value is not None:
            if isinstance(value, str):
                return _uuid_mod.UUID(value).hex
            return value.hex
        return value

    return _patched


_sa_types.Uuid.bind_processor = _uuid_bind_patched

from finance_sync.api.deps.auth import (
    APIKeyAuthResult,
    AuthContext,
    get_auth_context,
)
from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.db import Base
from finance_sync.dependencies import get_db
from finance_sync.models import (
    ConnectionAuditLog,
    SyncSchedule,
    Tenant,
)
from finance_sync.models.sync_schedule import SCOPE_EXPORT, SCOPE_INGESTION
from finance_sync.sync.schedule_spec import default_schedule

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI


def _make_settings() -> Settings:
    from pydantic import SecretStr

    return Settings(
        database_url=None,
        redis_url=None,
        secret_key=SecretStr("test-secret-key-at-least-16-chars"),
    )


@pytest.fixture
def db_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    asyncio.run(_create_all(engine))
    yield engine
    asyncio.run(engine.dispose())


async def _create_all(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def app(
    session_factory: async_sessionmaker[AsyncSession],
) -> Generator[FastAPI, None, None]:
    app = create_app(settings=_make_settings())
    app.dependency_overrides[get_db] = _override_db(session_factory)
    yield app


def _override_db(factory):
    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return _get_db


def _seed(
    factory: async_sessionmaker[AsyncSession],
    rows: list[Any],
) -> list[Any]:
    async def _run() -> list[Any]:
        async with factory() as session:
            for row in rows:
                session.add(row)
            await session.commit()
            # Refresh to populate server defaults (id, created_at).
            for row in rows:
                await session.refresh(row)
        return rows

    return asyncio.run(_run())


def _auth_override(tenant_id: str = "tenant-1", permissions: str | None = None):
    return lambda: AuthContext(
        api_key_result=APIKeyAuthResult(
            tenant_id=tenant_id,
            permissions=permissions,
        )
    )


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(id=str(uuid4()), slug="t1", name="Tenant 1")


@pytest.fixture
def schedule_row(tenant: Tenant) -> SyncSchedule:
    from finance_sync.services.sync_schedule import compute_next_run

    row = SyncSchedule(
        tenant_id=tenant.id,
        scope=SCOPE_INGESTION,
        target_id="aaaa1111-aaaa-4aaa-8aaa-aaaa11111111",
        enabled=True,
        schedule=default_schedule(),
        schema_version=1,
        timezone="Europe/Amsterdam",
        version=1,
    )
    instants = compute_next_run(row, count=1)
    row.next_run_at = instants[0] if instants else None
    return row


class TestScheduleApi:
    def test_list_requires_sync_read(
        self, app: FastAPI, session_factory
    ) -> None:
        app.dependency_overrides[get_auth_context] = _auth_override(
            permissions="transactions:read"
        )
        with TestClient(app) as client:
            resp = client.get("/api/v1/sync-schedules")
        assert resp.status_code == 403  # lacks sync:read

    def test_list_and_get_roundtrip(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            resp = client.get("/api/v1/sync-schedules")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            item = data["items"][0]
            assert item["scope"] == "ingestion"
            assert item["target_id"] == "aaaa1111-aaaa-4aaa-8aaa-aaaa11111111"
            assert item["enabled"] is True
            assert item["timezone"] == "Europe/Amsterdam"
            assert item["human_readable"] == "Elke werkdag om 07:00"
            assert item["version"] == 1
            assert item["next_run_at"] is not None
            # No credential/provider fields leak.
            for key in ("credentials", "payload", "encrypted_payload"):
                assert key not in item

            # GET detail
            detail = client.get(f"/api/v1/sync-schedules/{item['id']}")
            assert detail.status_code == 200
            assert detail.json()["id"] == item["id"]

    def test_scope_filter(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        export = SyncSchedule(
            tenant_id=tenant.id,
            scope=SCOPE_EXPORT,
            target_id="wealthfolio",
            enabled=True,
            schedule=default_schedule(),
            schema_version=1,
            timezone="UTC",
            version=1,
        )
        _seed(session_factory, [tenant, schedule_row, export])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            ing = client.get("/api/v1/sync-schedules?scope=ingestion").json()
            exp = client.get("/api/v1/sync-schedules?scope=export").json()
            bad = client.get("/api/v1/sync-schedules?scope=bogus")
        assert ing["total"] == 1 and ing["items"][0]["scope"] == "ingestion"
        assert exp["total"] == 1 and exp["items"][0]["scope"] == "export"
        assert bad.status_code == 422

    def test_foreign_schedule_is_uniform_404(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        other = str(uuid4())
        with TestClient(app) as client:
            # From tenant-1's perspective schedule_row.id exists; a
            # *different* tenant must see the same 404 as a random id.
            app.dependency_overrides[get_auth_context] = _auth_override(
                other, permissions="sync:read sync:write"
            )
            foreign_resp = client.get(
                f"/api/v1/sync-schedules/{schedule_row.id}"
            )
            random_resp = client.get(f"/api/v1/sync-schedules/{uuid4()}")
        assert foreign_resp.status_code == 404
        assert random_resp.status_code == 404
        assert foreign_resp.json() == random_resp.json()

    def test_foreign_schedule_404_on_all_endpoints(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            str(uuid4()), permissions="sync:read sync:write"
        )
        with TestClient(app) as client:
            for method, path in [
                ("get", f"/api/v1/sync-schedules/{schedule_row.id}"),
                ("get", f"/api/v1/sync-schedules/{schedule_row.id}/preview"),
                ("post", f"/api/v1/sync-schedules/{schedule_row.id}/reset"),
                ("post", f"/api/v1/sync-schedules/{schedule_row.id}/disable"),
                ("post", f"/api/v1/sync-schedules/{schedule_row.id}/enable"),
            ]:
                resp = getattr(client, method)(path)
                assert resp.status_code == 404, (
                    f"{method} {path} -> {resp.status_code}"
                )
            # PATCH needs a body to pass validation before the handler.
            resp = client.patch(
                f"/api/v1/sync-schedules/{schedule_row.id}",
                json={"enabled": False},
            )
            assert resp.status_code == 404

    def test_update_recomputes_next_run_and_audits(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/v1/sync-schedules/{schedule_row.id}",
                json={
                    "schedule": {"frequency": "daily", "time": "06:30"},
                    "timezone": "UTC",
                    "version": 1,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["schedule"]["frequency"] == "daily"
            assert data["timezone"] == "UTC"
            assert data["version"] == 2
            assert data["human_readable"] == "Elke dag om 06:30"

            # Audit record written with old/new schedule, no secrets.
            audits = asyncio.run(_load_audits(session_factory, tenant.id))
            assert any(a.action == "schedule.update" for a in audits)
            update = next(a for a in audits if a.action == "schedule.update")
            assert update.detail["old_schedule"]["frequency"] == "weekdays"
            assert update.detail["new_schedule"]["frequency"] == "daily"
            assert "token" not in str(update.detail)

    def test_stale_version_returns_409(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            # First update bumps to version 2.
            r1 = client.patch(
                f"/api/v1/sync-schedules/{schedule_row.id}",
                json={
                    "schedule": {"frequency": "daily", "time": "06:00"},
                    "version": 1,
                },
            )
            assert r1.status_code == 200
            # Stale version 1 again → 409, content unchanged.
            r2 = client.patch(
                f"/api/v1/sync-schedules/{schedule_row.id}",
                json={
                    "schedule": {"frequency": "hourly", "interval_hours": 3},
                    "version": 1,
                },
            )
            assert r2.status_code == 409
            detail = client.get(
                f"/api/v1/sync-schedules/{schedule_row.id}"
            ).json()
            assert detail["schedule"]["frequency"] == "daily"
            assert detail["version"] == 2

    def test_disable_stops_runs_and_enable_recomputes(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            disabled = client.post(
                f"/api/v1/sync-schedules/{schedule_row.id}/disable"
            )
            assert disabled.status_code == 200
            assert disabled.json()["enabled"] is False
            assert disabled.json()["next_run_at"] is None

            enabled = client.post(
                f"/api/v1/sync-schedules/{schedule_row.id}/enable"
            )
            assert enabled.status_code == 200
            assert enabled.json()["enabled"] is True
            assert enabled.json()["next_run_at"] is not None

    def test_reset_restores_default(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            client.patch(
                f"/api/v1/sync-schedules/{schedule_row.id}",
                json={"schedule": {"frequency": "hourly", "interval_hours": 6}},
            )
            reset = client.post(
                f"/api/v1/sync-schedules/{schedule_row.id}/reset"
            )
            assert reset.status_code == 200
            data = reset.json()
            assert data["schedule"]["frequency"] == "weekdays"
            assert data["timezone"] == "Europe/Amsterdam"
            assert data["enabled"] is True

    def test_preview_returns_three_instants(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            resp = client.get(
                f"/api/v1/sync-schedules/{schedule_row.id}/preview?count=3"
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["next_runs"]) == 3
            # Instants are strictly increasing UTC.
            parsed = [datetime.fromisoformat(d) for d in data["next_runs"]]
            assert parsed == sorted(parsed)
            assert all(d.tzinfo is not None for d in parsed)

    def test_validation_errors_are_consistent_422(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        bad_bodies = [
            {"schedule": {"frequency": "bogus"}},
            {
                "schedule": {
                    "frequency": "weekly",
                    "time": "07:00",
                    "weekdays": [],
                }
            },
            {"schedule": {"frequency": "hourly", "interval_hours": 0}},
            {"schedule": {"frequency": "hourly", "interval_hours": -5}},
            {"schedule": {"frequency": "hourly", "interval_hours": 1.5}},
            {"schedule": {"frequency": "hourly", "interval_hours": 10**9}},
            {"schedule": {"frequency": "daily", "time": "25:00"}},
            {"timezone": "Mars/Olympus"},
        ]
        with TestClient(app) as client:
            for body in bad_bodies:
                resp = client.patch(
                    f"/api/v1/sync-schedules/{schedule_row.id}", json=body
                )
                assert resp.status_code == 422, f"{body} -> {resp.status_code}"
                assert resp.json()["detail"]

    def test_update_requires_sync_write(
        self, app: FastAPI, session_factory, tenant, schedule_row
    ) -> None:
        _seed(session_factory, [tenant, schedule_row])
        app.dependency_overrides[get_auth_context] = _auth_override(
            tenant.id, permissions="sync:read sync:write connectors:write"
        )
        with TestClient(app) as client:
            # Read-only API key (sync:read only) can list but not write.

            readonly_ctx = AuthContext(
                api_key_result=APIKeyAuthResult(
                    tenant_id=tenant.id,
                    permissions="sync:read",
                )
            )
            app.dependency_overrides[get_auth_context] = lambda: readonly_ctx
            listed = client.get("/api/v1/sync-schedules")
            assert listed.status_code == 200
            written = client.patch(
                f"/api/v1/sync-schedules/{schedule_row.id}",
                json={"enabled": False},
            )
            assert written.status_code == 403

    # ═══════════════════════════════════════════════════════════════
    # POST /sync-schedules/preview — proposed-schedule preview
    # ═══════════════════════════════════════════════════════════════

    def test_proposed_preview_weekdays_three_instants(
        self, app: FastAPI, session_factory
    ) -> None:
        app.dependency_overrides[get_auth_context] = _auth_override(
            str(uuid4()), permissions="sync:read"
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/sync-schedules/preview",
                json={
                    "schedule": {
                        "frequency": "weekdays",
                        "time": "07:00",
                    },
                    "timezone": "UTC",
                    "count": 3,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == ""  # proposed, not stored
        assert len(data["next_runs"]) == 3
        parsed = [datetime.fromisoformat(d) for d in data["next_runs"]]
        assert parsed == sorted(parsed)
        assert all(d.tzinfo is not None for d in parsed)
        assert data["human_readable"] == "Elke werkdag om 07:00"
        assert data["timezone"] == "UTC"

    def test_proposed_preview_hourly_interval(
        self, app: FastAPI, session_factory
    ) -> None:
        app.dependency_overrides[get_auth_context] = _auth_override(
            str(uuid4()), permissions="sync:read"
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/sync-schedules/preview",
                json={
                    "schedule": {
                        "frequency": "hourly",
                        "interval_hours": 6,
                    },
                    "timezone": "Europe/Amsterdam",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["next_runs"]) == 3
        assert data["human_readable"] == "Elke 6 uur"

    def test_proposed_preview_validates_and_returns_422(
        self, app: FastAPI, session_factory
    ) -> None:
        app.dependency_overrides[get_auth_context] = _auth_override(
            str(uuid4()), permissions="sync:read"
        )
        bad_bodies = [
            {"schedule": {"frequency": "bogus"}},
            {
                "schedule": {
                    "frequency": "weekly",
                    "time": "07:00",
                    "weekdays": [],
                }
            },
            {"schedule": {"frequency": "hourly", "interval_hours": 0}},
            {"schedule": {"frequency": "daily", "time": "25:99"}},
            {
                "schedule": {"frequency": "daily", "time": "08:00"},
                "timezone": "Mars/Olympus",
            },
        ]
        with TestClient(app) as client:
            for body in bad_bodies:
                resp = client.post("/api/v1/sync-schedules/preview", json=body)
                assert resp.status_code == 422, f"{body} -> {resp.status_code}"
                assert resp.json()["detail"]

    def test_proposed_preview_requires_sync_read(
        self, app: FastAPI, session_factory
    ) -> None:
        app.dependency_overrides[get_auth_context] = _auth_override(
            str(uuid4()), permissions="transactions:read"
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/sync-schedules/preview",
                json={
                    "schedule": {"frequency": "daily", "time": "08:00"},
                    "timezone": "UTC",
                },
            )
        assert resp.status_code == 403

    def test_proposed_preview_default_count_is_three(
        self, app: FastAPI, session_factory
    ) -> None:
        app.dependency_overrides[get_auth_context] = _auth_override(
            str(uuid4()), permissions="sync:read"
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/sync-schedules/preview",
                json={
                    "schedule": {"frequency": "daily", "time": "09:00"},
                    "timezone": "UTC",
                },
            )
        assert resp.status_code == 200
        assert len(resp.json()["next_runs"]) == 3

    def test_proposed_preview_does_not_persist(
        self, app: FastAPI, session_factory
    ) -> None:
        app.dependency_overrides[get_auth_context] = _auth_override(
            str(uuid4()), permissions="sync:read"
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/sync-schedules/preview",
                json={
                    "schedule": {"frequency": "daily", "time": "08:00"},
                    "timezone": "UTC",
                },
            )
            assert resp.status_code == 200
            listed = client.get("/api/v1/sync-schedules")
        assert listed.status_code == 200
        assert listed.json()["total"] == 0


async def _load_audits(factory, tenant_id: str) -> list[ConnectionAuditLog]:
    async with factory() as session:
        rows = await session.scalars(
            select(ConnectionAuditLog).where(
                ConnectionAuditLog.tenant_id == tenant_id
            )
        )
        return list(rows.all())
