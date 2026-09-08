"""PostgreSQL-backed contract tests for the destination test endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.db.uow import UnitOfWork
from finance_sync.exporter.wealthfolio.models import WealthfolioAccountMapping
from finance_sync.models import Account, ExportTarget, Tenant, Transaction, User
from finance_sync.models.enums import UserRole
from finance_sync.services.auth import create_access_token, hash_password
from finance_sync.services.wealthfolio_preflight import (
    WealthfolioDestinationProbe,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

pytestmark = pytest.mark.integration

_SECRET = "destination-integration-secret-32chars!!"
_MASTER_KEY = "0123456789abcdef" * 4


@pytest.fixture
def destination_settings(database_url: str, redis_url: str) -> Settings:
    return Settings(
        database_url=database_url,  # pyright: ignore[reportArgumentType]
        redis_url=redis_url,  # pyright: ignore[reportArgumentType]
        secret_key=_SECRET,  # pyright: ignore[reportArgumentType]
        master_encryption_key=_MASTER_KEY,  # pyright: ignore[reportArgumentType]
    )


@pytest.fixture
def destination_container(
    destination_settings: Settings,
    pg_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> Container:
    container = Container.from_settings(destination_settings)
    container._engine = pg_engine  # pyright: ignore[reportPrivateUsage]
    container._session_factory = session_factory  # pyright: ignore[reportPrivateUsage]
    return container


@pytest.fixture
def destination_app(
    destination_settings: Settings, destination_container: Container
) -> Any:
    app = create_app(settings=destination_settings)
    app.state.container = destination_container
    return app


@pytest.fixture
async def destination_client(
    destination_app: Any,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=destination_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://integration"
    ) as client:
        yield client


async def _seed_admin(
    session_factory: async_sessionmaker[AsyncSession], slug: str
) -> dict[str, Any]:
    async with session_factory() as session:
        async with UnitOfWork(session) as uow:
            tenant = await uow.tenants.add(
                Tenant(slug=slug, name=f"Destination {slug}")
            )
            user = User(
                email=f"{slug}@finance-sync.local",
                tenant_id=str(tenant.id),
                hashed_password=hash_password("integration-password"),
                display_name=slug,
                role=UserRole.ADMIN,
                is_active=True,
            )
            uow.session.add(user)
        tenant_id = str(tenant.id)
        user_id = str(user.id)
    token = create_access_token(
        {"sub": user_id, "tenant_id": tenant_id, "role": "admin"},
        Settings(secret_key=_SECRET),  # pyright: ignore[reportArgumentType]
    )
    return {
        "tenant_id": tenant_id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


async def _create_target(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> str:
    response = await client.post(
        "/api/v1/destinations",
        headers=headers,
        json={
            "target_type": "wealthfolio",
            "display_name": "Integration Wealthfolio",
            "configuration": {"server_url": "http://127.0.0.1:3001"},
            "secret": {"password": "remote-password"},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _stored_target(
    session_factory: async_sessionmaker[AsyncSession], target_id: str
) -> ExportTarget:
    async with session_factory() as session:
        target = await session.scalar(
            select(ExportTarget).where(ExportTarget.id == target_id)
        )
        assert target is not None
        return target


@pytest.mark.parametrize(
    ("probe_status", "expected_status"),
    [
        ("ready", "ready"),
        ("unauthorized", "unauthorized"),
        ("unavailable", "unavailable"),
    ],
)
async def test_destination_test_persists_remote_probe_status(
    destination_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    probe_status: str,
    expected_status: str,
) -> None:
    seeded = await _seed_admin(
        session_factory, f"destination-probe-{probe_status}"
    )
    target_id = await _create_target(destination_client, seeded["headers"])
    probe = WealthfolioDestinationProbe(
        status=probe_status, reason=f"synthetic {probe_status}"
    )

    with patch(
        "finance_sync.api.v1.destinations.probe_wealthfolio_destination",
        new=AsyncMock(return_value=probe),
    ):
        response = await destination_client.post(
            f"/api/v1/destinations/{target_id}/test",
            headers=seeded["headers"],
        )

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert "remote-password" not in response.text
    target = await _stored_target(session_factory, target_id)
    assert target.last_health_status == expected_status
    assert target.last_health_error == (
        None if probe_status == "ready" else f"synthetic {probe_status}"
    )
    assert target.last_parity_summary["status"] == expected_status
    if probe_status == "ready":
        assert target.last_parity_summary["counts"] == {
            "remote_accounts": 0,
            "remote_assets": 0,
            "remote_activities": 0,
            "canonical_activities": 0,
            "unmapped_remote_accounts": 0,
            "stale_remote_activities": 0,
        }
    else:
        assert target.last_parity_summary["counts"] == {}


async def test_destination_test_reports_unmapped_remote_account(
    destination_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_admin(session_factory, "destination-unmapped")
    target_id = await _create_target(destination_client, seeded["headers"])
    probe = WealthfolioDestinationProbe(
        status="ready",
        accounts=({"id": "remote-account-1", "providerAccountId": "foreign"},),
    )

    with patch(
        "finance_sync.api.v1.destinations.probe_wealthfolio_destination",
        new=AsyncMock(return_value=probe),
    ):
        response = await destination_client.post(
            f"/api/v1/destinations/{target_id}/test",
            headers=seeded["headers"],
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["parity"]["unmapped_remote_accounts"] == 1
    target = await _stored_target(session_factory, target_id)
    assert target.last_health_status == "degraded"
    assert target.last_parity_summary == {
        "status": "degraded",
        "counts": {
            "remote_accounts": 1,
            "unmapped_remote_accounts": 1,
            "remote_assets": 0,
            "remote_activities": 0,
        },
    }


async def test_destination_test_reports_tombstoned_activity_as_stale(
    destination_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_admin(session_factory, "destination-stale")
    async with session_factory() as session:
        account = Account(
            id=uuid4(),
            tenant_id=seeded["tenant_id"],
            provider_key="trading212",
            external_account_id="provider-account",
            name="Portfolio",
            account_type="brokerage",
            currency_code="EUR",
        )
        session.add(account)
        await session.flush()
        target_id = await _create_target(destination_client, seeded["headers"])
        session.add(
            WealthfolioAccountMapping(
                tenant_id=seeded["tenant_id"],
                target_id=target_id,
                account_id=str(account.id),
                provider_account_id="provider-account",
                wf_account_name="Portfolio",
                wf_account_id="remote-account-1",
            )
        )
        session.add(
            Transaction(
                id=uuid4(),
                tenant_id=seeded["tenant_id"],
                provider_key="trading212",
                external_transaction_id="deleted-local",
                account_id=str(account.id),
                amount=1,
                currency_code="EUR",
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                transaction_type="deposit",
                status="booked",
                tombstoned_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
        await session.commit()

    probe = WealthfolioDestinationProbe(
        status="ready",
        accounts=(
            {
                "id": "remote-account-1",
                "providerAccountId": "provider-account",
            },
        ),
        activities={"remote-account-1": ({"sourceRecordId": "deleted-local"},)},
    )
    with patch(
        "finance_sync.api.v1.destinations.probe_wealthfolio_destination",
        new=AsyncMock(return_value=probe),
    ):
        response = await destination_client.post(
            f"/api/v1/destinations/{target_id}/test",
            headers=seeded["headers"],
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["parity"]["stale_remote_activities"] == 1
    assert body["parity_accounts"][0]["stale_remote_activities"] == 1
    target = await _stored_target(session_factory, target_id)
    assert target.last_parity_summary["counts"]["stale_remote_activities"] == 1


async def test_destination_probe_can_be_disabled_without_remote_call(
    destination_app: Any,
    destination_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_admin(session_factory, "destination-disabled")
    target_id = await _create_target(destination_client, seeded["headers"])
    destination_app.state.container.settings.destination_remote_probe_enabled = False
    mock_probe = AsyncMock()

    with patch(
        "finance_sync.api.v1.destinations.probe_wealthfolio_destination",
        new=mock_probe,
    ):
        response = await destination_client.post(
            f"/api/v1/destinations/{target_id}/test",
            headers=seeded["headers"],
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    mock_probe.assert_not_awaited()
    target = await _stored_target(session_factory, target_id)
    assert target.last_health_status == "disabled"
    assert target.last_parity_summary == {
        "status": "disabled",
        "counts": {},
    }


async def test_destination_probe_rate_limit_is_target_scoped(
    destination_app: Any,
    destination_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = destination_app.state.container.settings
    settings.destination_probe_rate_limit_max_requests = 1
    settings.destination_probe_rate_limit_window_seconds = 60
    seeded = await _seed_admin(session_factory, "destination-rate-limit")
    target_id = await _create_target(destination_client, seeded["headers"])
    probe = WealthfolioDestinationProbe(status="ready")

    with patch(
        "finance_sync.api.v1.destinations.probe_wealthfolio_destination",
        new=AsyncMock(return_value=probe),
    ):
        first = await destination_client.post(
            f"/api/v1/destinations/{target_id}/test",
            headers=seeded["headers"],
        )
        second = await destination_client.post(
            f"/api/v1/destinations/{target_id}/test",
            headers=seeded["headers"],
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"]["limit"] == 1
