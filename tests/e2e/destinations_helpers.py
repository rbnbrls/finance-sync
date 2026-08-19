"""Shared helpers for the destination-wizard E2E suite."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

import httpx
import pytest

from finance_sync.db.uow import UnitOfWork
from finance_sync.models import Tenant, User
from finance_sync.models.enums import UserRole
from finance_sync.services.auth import create_access_token, hash_password

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.config.settings import Settings


class SeededTenant(TypedDict):
    tenant_id: str
    user_id: str
    headers: dict[str, str]


@pytest.fixture
async def seeded_destination_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    e2e_settings: Settings,
) -> SeededTenant:
    """A single-owner tenant + admin user with valid JWT headers."""
    async with session_factory() as session, UnitOfWork(session) as uow:
        tenant = await uow.tenants.add(
            Tenant(slug="dest-tenant", name="Destination Tenant")
        )
        user = User(
            email="dest@finance-sync.local",
            tenant_id=str(tenant.id),
            hashed_password=hash_password("dest-password"),
            display_name="Destination Owner",
            role=UserRole.ADMIN,
            is_active=True,
        )
        uow.session.add(user)
    token = create_access_token(
        {"sub": str(user.id), "tenant_id": str(tenant.id), "role": "admin"},
        e2e_settings,
    )
    return {
        "tenant_id": str(tenant.id),
        "user_id": str(user.id),
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
async def dest_client(
    e2e_app: FastAPI,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=e2e_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://dest"
    ) as client:
        yield client


async def create_destination(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    target_type: str,
    display_name: str,
    configuration: dict[str, Any] | None = None,
    secret: dict[str, str] | None = None,
    selected_account_ids: list[str] | None = None,
    datasets: list[str] | None = None,
) -> dict[str, Any]:
    """Create a destination (draft) via the wizard API."""
    default_datasets = (
        ["accounts", "transactions", "holdings", "securities", "prices"]
        if target_type == "jupyter"
        else ["accounts", "transactions"]
    )
    payload: dict[str, Any] = {
        "target_type": target_type,
        "display_name": display_name,
        "configuration": configuration or {},
        "datasets": datasets or default_datasets,
    }
    if secret:
        payload["secret"] = secret
    if selected_account_ids is not None:
        payload["selected_account_ids"] = selected_account_ids
    resp = await client.post(
        "/api/v1/destinations", json=payload, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
