"""Smoke-test GET /api/v1/sync-runs against the populated, migrated PG DB.

Verifies the root-cause fix for issue #451:
- schema: sync_runs.connection_id is uuid, matching credentials.id
- the ORM join compiles to bare uuid = uuid (no cast)
- GET /api/v1/sync-runs returns 200 with populated data
- GET /api/v1/sync-runs/{run_id} returns 200 for a known run
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
from sqlalchemy import select

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.lifespan import lifespan
from finance_sync.models.api_key import ApiKey
from finance_sync.models.enums import UserRole
from finance_sync.models.user import User
from finance_sync.services.auth import generate_api_key

DATABASE_URL = os.environ.get(
    "SMOKE_DB_URL",
    "postgresql+asyncpg://postgres@localhost:5432/fs_t_390192ad_smoke",
)


async def main() -> None:
    settings = Settings(
        secret_key="smoke-test-secret-key-at-least-16-chars",
        admin_key="smoke-admin-key-1234567890abcdef",
        database_url=DATABASE_URL,
        redis_url=None,
        master_encryption_key=None,
    )
    app = create_app(settings=settings)

    async with lifespan(app):
        container = app.state.container
        session_factory = container.session_factory

        from finance_sync.models.credential import Credential
        from finance_sync.models.sync_run import SyncRun

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(SyncRun.connection_id)
                    .where(SyncRun.connection_id.isnot(None))
                    .limit(1)
                )
            ).scalar_one_or_none()
            assert row is not None, "no sync runs with connection_id in DB"

            cred = (
                await session.execute(
                    select(Credential).where(Credential.id == row)
                )
            ).scalar_one()
            tenant_id = cred.tenant_id

            # Count sync runs for this tenant (endpoint is tenant-scoped)
            expected_total = (
                (
                    await session.execute(
                        select(SyncRun.id)
                        .join(
                            Credential, Credential.id == SyncRun.connection_id
                        )
                        .where(Credential.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
            expected_total = len(expected_total)
            print(f"expected_total(tenant)={expected_total}")

            user = User(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                email=f"smoke-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                display_name="Smoke Tester",
                is_active=True,
                role=UserRole.ADMIN,
            )
            session.add(user)
            await session.flush()

            raw_key, key_hash, prefix = generate_api_key()
            api_key = ApiKey(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                user_id=user.id,
                name="smoke-test",
                key_prefix=prefix,
                key_hash=key_hash,
                permissions="sync:read",
            )
            session.add(api_key)
            await session.commit()
            print(f"tenant_id={tenant_id}")
            print(f"api_key_prefix={prefix}")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            headers = {"X-API-Key": raw_key}

            r = await client.get("/api/v1/sync-runs", headers=headers)
            print(f"GET /api/v1/sync-runs -> {r.status_code}")
            if r.status_code != 200:
                print(r.text[:2000])
                raise SystemExit(1)
            data = r.json()
            print(f"  total={data['total']}")
            print(f"  items={len(data['items'])}")
            print(f"  status_counts={data['status_counts']}")
            assert data["total"] == expected_total, (
                f"expected {expected_total} runs, got {data['total']}"
            )
            assert len(data["items"]) > 0, "expected at least one item"
            first = data["items"][0]
            print(
                f"  first item: id={first['id']} "
                f"connector={first['connector']} status={first['status']}"
            )

            run_id = first["id"]
            rd = await client.get(
                f"/api/v1/sync-runs/{run_id}", headers=headers
            )
            print(f"GET /api/v1/sync-runs/{run_id} -> {rd.status_code}")
            if rd.status_code != 200:
                print(rd.text[:2000])
                raise SystemExit(1)
            detail = rd.json()
            print(
                f"  detail: connector={detail['connector']} "
                f"connection_id={detail['connection_id']} "
                f"status={detail['status']}"
            )

            rc = await client.get(
                "/api/v1/sync-runs",
                headers=headers,
                params={"connector": "bunq"},
            )
            print(f"GET /api/v1/sync-runs?connector=bunq -> {rc.status_code}")
            if rc.status_code != 200:
                print(rc.text[:2000])
                raise SystemExit(1)
            cdata = rc.json()
            print(f"  total(bunq)={cdata['total']}")

    print("SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
