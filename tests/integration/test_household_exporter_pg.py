"""PG integration: exporter visibility + revocation cleanup for household sharing.

Scope: kanban task t_2d535426 — the Wealthfolio and Actual Budget
exporters must strictly export accounts with ``household`` visibility.
Private accounts are never created or updated in the shared instances;
revoking a share (visibility → private) stops further export on the
next run.  This file is the counterpart of
``test_household_sharing_pg.py`` (t_2b794533 scope) and lives in the
shared workspace uncommitted so the exporter task picks it up together
with the exporter source changes.

Revocation cleanup: unsharing an account that had previously exported
data (Wealthfolio mapping row, delivery cursor, CSV files on disk)
*triggers* a user-confirmed cleanup/quarantine flow — the unshare
response reports ``export_cleanup_required``, nothing is deleted
silently, and the owner must explicitly quarantine (non-destructive
move of CSV files) or delete (destructive, requires ``confirm: true``)
the already-exported artifacts.  Every decision lands in the
tenant-scoped household audit log as ``account_export_quarantine``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import httpx

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.exporter.wealthfolio.models import (
    WealthfolioAccountMapping,
    WealthfolioDelivery,
)
from finance_sync.models import Account, Tenant, User
from finance_sync.models.enums import AccountVisibility, UserRole
from finance_sync.models.household_audit_log import (
    AUDIT_ACCOUNT_EXPORT_QUARANTINE,
    HouseholdAuditLog,
)
from finance_sync.services.auth import create_access_token, hash_password

pytestmark = pytest.mark.integration

_INT_SECRET = "household-int-secret-key-16chars"
_INT_MASTER_KEY = "cd" * 32  # 64 hex chars → 32-byte AES-256 key

_API_PREFIX = "/api/v1"


# ── App fixtures (same wiring as test_household_sharing_pg) ──────────


@pytest.fixture
def api_settings(database_url: str, redis_url: str, tmp_path: Path) -> Settings:
    """Settings pointing the app at the harness PG + a temp export dir."""
    return Settings(
        database_url=database_url,  # pyright: ignore[reportArgumentType]
        redis_url=redis_url,  # pyright: ignore[reportArgumentType]
        secret_key=_INT_SECRET,  # pyright: ignore[reportArgumentType]
        master_encryption_key=_INT_MASTER_KEY,  # pyright: ignore[reportArgumentType]
        wealthfolio_output_dir=str(tmp_path / "wf_exports"),
    )


@pytest.fixture
def api_container(
    api_settings: Settings,
    pg_engine: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> Container:
    """DI container bound to the harness engine (NullPool, loop-safe)."""
    container = Container.from_settings(api_settings)
    container._engine = pg_engine  # pyright: ignore[reportPrivateUsage]
    container._session_factory = session_factory  # pyright: ignore[reportPrivateUsage]
    return container


@pytest.fixture
def api_app(api_settings: Settings, api_container: Container) -> FastAPI:
    """FastAPI app with the container attached (lifespan not run)."""
    app = create_app(settings=api_settings)
    app.state.container = api_container
    return app


@pytest.fixture
async def api_client(
    api_app: FastAPI,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client against the in-process app."""
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://integration"
    ) as client:
        yield client


# ── Seeding helpers ───────────────────────────────────────────────────


async def _seed_tenant_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    role: UserRole,
    email: str | None = None,
    tenant: Tenant | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Persist a tenant (unless given) + user and return a signed JWT."""
    email = email or f"{slug}@finance-sync.local"
    async with session_factory() as session:
        if tenant is None and tenant_id is not None:
            tenant = (
                await session.execute(
                    select(Tenant).where(Tenant.id == tenant_id)
                )
            ).scalar_one()
        if tenant is None:
            tenant = Tenant(slug=slug, name=f"Household {slug}")
        session.add(tenant)
        await session.flush()
        user = User(
            email=email,
            tenant_id=str(tenant.id),
            hashed_password=hash_password("integration-password"),
            display_name=f"User {slug}",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        tenant_id_out = str(tenant.id)
        user_id = str(user.id)

    token = create_access_token(
        {"sub": user_id, "tenant_id": tenant_id_out, "role": role},
        Settings(secret_key=_INT_SECRET),  # pyright: ignore[reportArgumentType]
    )
    return {
        "tenant_id": tenant_id_out,
        "user_id": user_id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


async def _seed_account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    owner_user_id: str | None,
    name: str,
    visibility: str = AccountVisibility.PRIVATE.value,
    balance: str = "1000.00",
    account_type: str = "checking",
) -> str:
    """Create an account row and return its id."""
    async with session_factory() as session:
        account = Account(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            provider_key="bunq",
            external_account_id=str(uuid4()),
            name=name,
            account_type=account_type,
            currency_code="EUR",
            current_balance=Decimal(balance),
            visibility=visibility,
        )
        session.add(account)
        await session.commit()
        return str(account.id)


async def _seed_export_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    account_id: str,
    output_dir: Path,
    wf_account_name: str = "Shared",
) -> list[Path]:
    """Create a Wealthfolio mapping + delivery cursor + CSV files.

    Mirrors what a real push/export leaves behind so the cleanup flow
    has something to describe, quarantine or delete.
    """
    safe = "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in wf_account_name
    )
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    files = [
        output_dir / f"transactions_{safe}_{ts}.csv",
        output_dir / f"holdings_{safe}_{ts}.csv",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("date,symbol,quantity\n", encoding="utf-8")

    async with session_factory() as session:
        session.add(
            WealthfolioAccountMapping(
                tenant_id=tenant_id,
                account_id=account_id,
                wf_account_name=wf_account_name,
                wf_account_id="wf-acct-1",
                provider_account_id=f"finance-sync:{tenant_id}:{account_id}",
            )
        )
        session.add(
            WealthfolioDelivery(
                tenant_id=tenant_id,
                account_id=account_id,
                last_exported_transaction_id=str(uuid4()),
                last_exported_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return files


async def _count_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    action: str,
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(HouseholdAuditLog).where(
                HouseholdAuditLog.tenant_id == tenant_id,
                HouseholdAuditLog.action == action,
            )
        )
        return len(list(result.scalars().all()))


# ═══════════════════════════════════════════════════════════════════════
# Exporters: only household accounts leave the instance
# ═══════════════════════════════════════════════════════════════════════


class TestExporterVisibility:
    @pytest.mark.asyncio
    async def test_wealthfolio_exporter_excludes_private_accounts(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
        from finance_sync.exporter.wealthfolio.exporter import (
            WealthfolioExporter,
        )

        admin = await _seed_tenant_user(
            session_factory, slug="hh-exp-admin", role=UserRole.ADMIN
        )
        await _seed_tenant_user(
            session_factory,
            slug="hh-exp-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        household_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
            account_type="investment",
        )
        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Private",
            account_type="investment",
        )

        exporter = WealthfolioExporter(
            session_factory=session_factory,
            wf_config=WealthfolioConfig(),  # default output dir is fine
            tenant_id=tenant_id,
        )
        accounts = await exporter._load_accounts(None)  # type: ignore[attr-defined]
        ids = {str(a.id) for a in accounts}
        assert household_acct in ids
        assert private_acct not in ids

    @pytest.mark.asyncio
    async def test_actual_budget_exporter_excludes_private_accounts(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from finance_sync.exporter.actual_budget.config import (
            ActualBudgetConfig,
        )
        from finance_sync.exporter.actual_budget.exporter import (
            ActualBudgetExporter,
        )

        admin = await _seed_tenant_user(
            session_factory, slug="hh-abexp-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]

        household_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
            account_type="checking",
        )
        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Private",
            account_type="checking",
        )

        exporter = ActualBudgetExporter(
            session_factory=session_factory,
            ab_config=ActualBudgetConfig(),
            tenant_id=tenant_id,
        )
        accounts = await exporter._load_accounts(None)  # type: ignore[attr-defined]
        ids = {str(a.id) for a in accounts}
        assert household_acct in ids
        assert private_acct not in ids

    @pytest.mark.asyncio
    async def test_revoke_stops_export_immediately(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
        from finance_sync.exporter.wealthfolio.exporter import (
            WealthfolioExporter,
        )

        admin = await _seed_tenant_user(
            session_factory, slug="hh-revexp-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]
        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Was Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
            account_type="investment",
        )

        exporter = WealthfolioExporter(
            session_factory=session_factory,
            wf_config=WealthfolioConfig(),  # default output dir is fine
            tenant_id=tenant_id,
        )
        assert acct_id in {
            str(a.id)
            for a in await exporter._load_accounts(None)  # type: ignore[attr-defined]
        }

        # Make private (revoke) → next export run excludes it
        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.id == acct_id)
                )
            ).scalar_one()
            account.visibility = AccountVisibility.PRIVATE.value
            await session.commit()

        assert acct_id not in {
            str(a.id)
            for a in await exporter._load_accounts(None)  # type: ignore[attr-defined]
        }


# ═══════════════════════════════════════════════════════════════════════
# Revocation cleanup: unshare triggers user-confirmed quarantine/delete
# ═══════════════════════════════════════════════════════════════════════


class TestRevocationCleanup:
    @pytest.mark.asyncio
    async def test_unshare_reports_cleanup_required_without_deleting(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Unshare reports exported artifacts but never deletes silently."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-rev-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]
        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared Acct",
            visibility=AccountVisibility.HOUSEHOLD.value,
            account_type="investment",
        )
        files = await _seed_export_artifacts(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            output_dir=tmp_path / "wf_exports",
        )

        resp = await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=admin["headers"],
            json={"visibility": "private"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["visibility"] == "private"
        assert body["export_cleanup_required"] is True
        artifacts = body["export_artifacts"]
        assert artifacts["has_mapping"] is True
        assert artifacts["has_delivery_cursor"] is True
        assert artifacts["csv_file_count"] == 2

        # Nothing was deleted or moved — data is still in place
        for path in files:
            assert path.exists(), f"{path.name} must not be deleted on unshare"

    @pytest.mark.asyncio
    async def test_unshare_without_artifacts_reports_no_cleanup(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An account that was never exported needs no cleanup."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-rev2-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]
        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Never Exported",
            visibility=AccountVisibility.HOUSEHOLD.value,
            account_type="investment",
        )

        resp = await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=admin["headers"],
            json={"visibility": "private"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["export_cleanup_required"] is False

    @pytest.mark.asyncio
    async def test_quarantine_moves_files_and_keeps_mapping(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Quarantine moves CSV files aside (non-destructive) and keeps
        the mapping/delivery rows so a future re-share resumes cleanly."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-q-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]
        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Quarantine Me",
            visibility=AccountVisibility.PRIVATE.value,
            account_type="investment",
        )
        files = await _seed_export_artifacts(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            output_dir=tmp_path / "wf_exports",
        )

        resp = await api_client.post(
            f"{_API_PREFIX}/accounts/{acct_id}/export-quarantine",
            headers=admin["headers"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["quarantined_files"] == 2

        # Originals gone from the active dir, present under quarantine/
        for path in files:
            assert not path.exists()
        quarantine_dir = tmp_path / "wf_exports" / "quarantine"
        quarantined = list(quarantine_dir.rglob("*.csv"))
        assert len(quarantined) == 2

        # Mapping + delivery rows survive quarantine
        async with session_factory() as session:
            mapping = (
                await session.execute(
                    select(WealthfolioAccountMapping).where(
                        WealthfolioAccountMapping.account_id == acct_id
                    )
                )
            ).scalar_one_or_none()
            delivery = (
                await session.execute(
                    select(WealthfolioDelivery).where(
                        WealthfolioDelivery.account_id == acct_id
                    )
                )
            ).scalar_one_or_none()
        assert mapping is not None
        assert delivery is not None

        # Audited
        assert (
            await _count_audit(
                session_factory,
                tenant_id=tenant_id,
                action=AUDIT_ACCOUNT_EXPORT_QUARANTINE,
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_delete_requires_confirmation_and_removes_everything(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Delete is destructive and therefore requires ``confirm: true``;
        it removes files AND the mapping/delivery rows, and is audited."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-del-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]
        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Delete Me",
            visibility=AccountVisibility.PRIVATE.value,
            account_type="investment",
        )
        files = await _seed_export_artifacts(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            output_dir=tmp_path / "wf_exports",
        )

        # Without confirmation → rejected, nothing touched
        resp = await api_client.post(
            f"{_API_PREFIX}/accounts/{acct_id}/export-cleanup",
            headers=admin["headers"],
            json={"confirm": False},
        )
        assert resp.status_code == 422
        for path in files:
            assert path.exists()

        # With confirmation → everything removed
        resp = await api_client.post(
            f"{_API_PREFIX}/accounts/{acct_id}/export-cleanup",
            headers=admin["headers"],
            json={"confirm": True},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted_files"] == 2

        for path in files:
            assert not path.exists()
        async with session_factory() as session:
            mapping = (
                await session.execute(
                    select(WealthfolioAccountMapping).where(
                        WealthfolioAccountMapping.account_id == acct_id
                    )
                )
            ).scalar_one_or_none()
            delivery = (
                await session.execute(
                    select(WealthfolioDelivery).where(
                        WealthfolioDelivery.account_id == acct_id
                    )
                )
            ).scalar_one_or_none()
        assert mapping is None
        assert delivery is None

        assert (
            await _count_audit(
                session_factory,
                tenant_id=tenant_id,
                action=AUDIT_ACCOUNT_EXPORT_QUARANTINE,
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_cleanup_is_owner_only(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Another household member cannot quarantine or delete the
        owner's exported artifacts (404-equivalent, no existence leak)."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-rbac-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-rbac-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]
        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner's",
            visibility=AccountVisibility.PRIVATE.value,
            account_type="investment",
        )
        await _seed_export_artifacts(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            output_dir=tmp_path / "wf_exports",
        )

        for endpoint in ("export-quarantine", "export-cleanup"):
            resp = await api_client.post(
                f"{_API_PREFIX}/accounts/{acct_id}/{endpoint}",
                headers=member["headers"],
                json={"confirm": True},
            )
            assert resp.status_code == 404, endpoint

        # Owner still intact
        async with session_factory() as session:
            mapping = (
                await session.execute(
                    select(WealthfolioAccountMapping).where(
                        WealthfolioAccountMapping.account_id == acct_id
                    )
                )
            ).scalar_one_or_none()
        assert mapping is not None
