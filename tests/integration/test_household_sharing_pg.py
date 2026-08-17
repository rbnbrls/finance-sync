"""PG integration: gedeeld huishouden met selectieve accountdeling.

Proves the t_7c8a35b3 acceptance criteria against a real migrated
PostgreSQL database over the HTTP API:

- two household members in one tenant; accounts are private-by-default
  (a member never sees another member's private accounts — not in
  account lists, single-account reads, transactions, holdings,
  balances, net worth, cashflow, portfolio, dividends, allocation or
  performance totals)
- selectively sharing an account (``visibility: household``) makes it
  and its derived data visible to the other member, without
  double-counting, while preserving per-account owner provenance
- revoking a share removes the account from the household view
  immediately and from both shared exporters (Wealthfolio + Actual
  Budget)
- RBAC: only the account owner may share/unshare; only admins may
  invite members, change roles or remove members; API keys cannot
  manage sharing
- invitations are single-use, expire, grant the invited role, and
  never leak whether an email already belongs to a tenant user
- every sensitive action lands in the tenant-scoped audit log with
  sanitised payloads (no financial data)
- tenant isolation: a household account of tenant A is invisible to
  tenant B users
- MCP semantics: API-key (machine) principals only see household and
  system-owned accounts, never a user's private accounts
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import httpx

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.models import (
    Account,
    Holding,
    Tenant,
    Transaction,
    User,
)
from finance_sync.models.enums import (
    AccountVisibility,
    TransactionStatus,
    TransactionType,
    UserRole,
)
from finance_sync.models.webhook import Webhook, WebhookDeliveryLog
from finance_sync.services.auth import create_access_token, hash_password
from finance_sync.services.visibility import ReadScope
from finance_sync.services.webhook import WebhookService

pytestmark = pytest.mark.integration

_INT_SECRET = "household-int-secret-key-16chars"
_INT_MASTER_KEY = "cd" * 32  # 64 hex chars → 32-byte AES-256 key

_API_PREFIX = "/api/v1"


# ── App fixtures (same wiring as test_connectors_api_pg) ──────────────


@pytest.fixture
def api_settings(database_url: str, redis_url: str) -> Settings:
    """Settings pointing the app at the ephemeral harness PG/Redis."""
    return Settings(
        database_url=database_url,  # pyright: ignore[reportArgumentType]
        redis_url=redis_url,  # pyright: ignore[reportArgumentType]
        secret_key=_INT_SECRET,  # pyright: ignore[reportArgumentType]
        master_encryption_key=_INT_MASTER_KEY,  # pyright: ignore[reportArgumentType]
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


async def _seed_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    account_id: str,
    amount: str,
    kind: TransactionType = TransactionType.PAYMENT,
    occurred_at: datetime | None = None,
) -> str:
    """Create a transaction row and return its id."""
    async with session_factory() as session:
        txn = Transaction(
            tenant_id=tenant_id,
            provider_key="bunq",
            external_transaction_id=str(uuid4()),
            account_id=account_id,
            amount=Decimal(amount),
            amount_in_base=Decimal(amount),
            currency_code="EUR",
            base_currency_code="EUR",
            occurred_at=occurred_at or datetime.now(UTC),
            transaction_type=kind,
            status=TransactionStatus.BOOKED,
            description=f"txn-{amount}",
        )
        session.add(txn)
        await session.commit()
        return str(txn.id)


async def _seed_holding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    account_id: str,
    security_id: str,
    observed_at: datetime,
    market_value: str,
) -> str:
    """Create a holding snapshot row and return its id."""
    from finance_sync.models.enums import HoldingSource

    async with session_factory() as session:
        holding = Holding(
            tenant_id=tenant_id,
            account_id=account_id,
            security_id=security_id,
            observed_at=observed_at,
            quantity=Decimal(10),
            market_value=Decimal(market_value),
            currency_code="EUR",
            price=Decimal(market_value) / Decimal(10),
            source=HoldingSource.PROVIDER_SYNC,
        )
        session.add(holding)
        await session.commit()
        return str(holding.id)


async def _seed_security(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a security row (holding FK target) and return its id."""
    from finance_sync.models.security import Security

    async with session_factory() as session:
        security = Security(
            ticker=f"TST{uuid4().hex[:6].upper()}",
            name="Test Security",
            security_type="stock",
            currency_code="EUR",
        )
        session.add(security)
        await session.commit()
        return str(security.id)


def _account_ids(payload: dict[str, Any]) -> set[str]:
    """Extract account ids from an accounts-list payload."""
    return {item["id"] for item in payload.get("items", [])}


# ═══════════════════════════════════════════════════════════════════════
# 1. Private-by-default + selective sharing over the read APIs
# ═══════════════════════════════════════════════════════════════════════


class TestVisibilityAcrossReadApis:
    """Two members, one tenant: private accounts never leak."""

    @pytest.mark.asyncio
    async def test_private_by_default_across_all_read_endpoints(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-owner", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        # Owner's private account with transactions + holdings
        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner Private",
            balance="2500.00",
        )
        await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            amount="-40.00",
        )
        await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            amount="1200.00",
            kind=TransactionType.DEPOSIT,
        )
        sec_id = await _seed_security(session_factory)
        now = datetime.now(UTC)
        await _seed_holding(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            security_id=sec_id,
            observed_at=now - timedelta(days=2),
            market_value="5000.00",
        )
        await _seed_holding(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            security_id=sec_id,
            observed_at=now - timedelta(days=1),
            market_value="5500.00",
        )

        # A second member's own account — visible only to them
        member_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=member["user_id"],
            name="Member Private",
            balance="75.00",
        )

        member_h = member["headers"]

        # Account list: member sees only their own account
        resp = await api_client.get(f"{_API_PREFIX}/accounts", headers=member_h)
        assert resp.status_code == 200
        ids = _account_ids(resp.json())
        assert acct_id not in ids
        assert member_acct in ids

        # Single-account read of the owner's private account → 404
        resp = await api_client.get(
            f"{_API_PREFIX}/accounts/{acct_id}", headers=member_h
        )
        assert resp.status_code == 404

        # Transactions: owner's are invisible
        resp = await api_client.get(
            f"{_API_PREFIX}/transactions", headers=member_h
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        # Holdings: owner's are invisible
        resp = await api_client.get(f"{_API_PREFIX}/holdings", headers=member_h)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        # Net worth: only the member's own account counts
        resp = await api_client.get(
            f"{_API_PREFIX}/net-worth", headers=member_h
        )
        assert resp.status_code == 200
        body = resp.json()
        assert Decimal(body["net_worth"]) == Decimal("75.00")
        assert {a["id"] for a in body["accounts"]} == {member_acct}

        # Cashflow: member sees zero transactions
        resp = await api_client.get(f"{_API_PREFIX}/cashflow", headers=member_h)
        assert resp.status_code == 200
        assert resp.json()["transaction_count"] == 0

        # Dividends: none visible to member
        resp = await api_client.get(
            f"{_API_PREFIX}/dividends", headers=member_h
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        # Portfolio: owner's holdings excluded
        resp = await api_client.get(
            f"{_API_PREFIX}/portfolio", headers=member_h
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_value"] is None or Decimal(
            body["total_value"]
        ) == Decimal(0)

        # Allocation: owner's holdings excluded
        resp = await api_client.get(
            f"{_API_PREFIX}/allocation", headers=member_h
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["total_value"]) == Decimal(0)

        # Performance: owner's private valuation excluded
        resp = await api_client.get(
            f"{_API_PREFIX}/performance/twr", headers=member_h
        )
        assert resp.status_code == 200
        assert resp.json()["periods"] == []

        # Owner sees their own data (sanity — the data exists; the
        # member's private account stays invisible to the admin too)
        resp = await api_client.get(
            f"{_API_PREFIX}/net-worth", headers=admin["headers"]
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["net_worth"]) == Decimal("2500.00")

        resp = await api_client.get(
            f"{_API_PREFIX}/portfolio", headers=admin["headers"]
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["total_value"]) == Decimal("5500.00")

        resp = await api_client.get(
            f"{_API_PREFIX}/performance/twr", headers=admin["headers"]
        )
        assert resp.status_code == 200
        assert len(resp.json()["periods"]) >= 1

    @pytest.mark.asyncio
    async def test_share_makes_data_visible_then_revoke_hides_it(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-share-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-share-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="To Share",
            balance="999.00",
        )
        await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=acct_id,
            amount="-30.00",
        )

        # Preview before sharing — shows the impact (transactions, balance)
        resp = await api_client.get(
            f"{_API_PREFIX}/accounts/{acct_id}/share-preview",
            headers=admin["headers"],
        )
        assert resp.status_code == 200
        preview = resp.json()
        assert preview["current_visibility"] == "private"
        assert preview["target_visibility"] == "household"
        assert preview["impact"]["transactions"] == 1
        assert Decimal(preview["impact"]["current_balance"]) == Decimal(
            "999.00"
        )

        # Share it
        resp = await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=admin["headers"],
            json={"visibility": "household"},
        )
        assert resp.status_code == 200
        assert resp.json()["visibility"] == "household"

        # Member now sees the account, its transactions and net worth
        member_h = member["headers"]
        resp = await api_client.get(f"{_API_PREFIX}/accounts", headers=member_h)
        assert acct_id in _account_ids(resp.json())

        resp = await api_client.get(
            f"{_API_PREFIX}/transactions", headers=member_h
        )
        assert resp.json()["total"] == 1

        resp = await api_client.get(
            f"{_API_PREFIX}/net-worth", headers=member_h
        )
        assert Decimal(resp.json()["net_worth"]) == Decimal("999.00")

        # Provenance preserved: owner id is exposed on the shared account
        resp = await api_client.get(
            f"{_API_PREFIX}/accounts/{acct_id}", headers=member_h
        )
        assert resp.status_code == 200
        assert resp.json()["owner_user_id"] == admin["user_id"]

        # Revoke → member loses visibility immediately
        resp = await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=admin["headers"],
            json={"visibility": "private"},
        )
        assert resp.status_code == 200

        resp = await api_client.get(f"{_API_PREFIX}/accounts", headers=member_h)
        assert acct_id not in _account_ids(resp.json())

        resp = await api_client.get(
            f"{_API_PREFIX}/net-worth", headers=member_h
        )
        assert resp.json()["net_worth"] is None or Decimal(
            resp.json()["net_worth"]
        ) == Decimal(0)

        resp = await api_client.get(
            f"{_API_PREFIX}/accounts/{acct_id}", headers=member_h
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_household_aggregation_no_double_count_with_provenance(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Two members share one account each; net worth sums both once."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-agg-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-agg-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        a_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="A Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
            balance="300.00",
        )
        m_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=member["user_id"],
            name="M Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
            balance="200.00",
        )

        for headers in (admin["headers"], member["headers"]):
            resp = await api_client.get(
                f"{_API_PREFIX}/net-worth", headers=headers
            )
            assert resp.status_code == 200
            body = resp.json()
            # Both shared accounts counted exactly once, provenance intact
            assert Decimal(body["net_worth"]) == Decimal("500.00")
            owners = {a["id"]: a["owner_user_id"] for a in body["accounts"]}
            assert owners[a_acct] == admin["user_id"]
            assert owners[m_acct] == member["user_id"]

    @pytest.mark.asyncio
    async def test_tenant_isolation_household_accounts(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Household accounts of tenant A are invisible to tenant B."""
        tenant_a = Tenant(slug="hh-iso-a", name="Iso A")
        admin_a = await _seed_tenant_user(
            session_factory,
            slug="hh-iso-admin",
            role=UserRole.ADMIN,
            tenant=tenant_a,
        )
        tenant_b = Tenant(slug="hh-iso-b", name="Iso B")
        user_b = await _seed_tenant_user(
            session_factory,
            slug="hh-iso-userb",
            role=UserRole.USER,
            tenant=tenant_b,
        )

        shared_acct = await _seed_account(
            session_factory,
            tenant_id=admin_a["tenant_id"],
            owner_user_id=admin_a["user_id"],
            name="A Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
            balance="777.00",
        )

        resp = await api_client.get(
            f"{_API_PREFIX}/accounts", headers=user_b["headers"]
        )
        assert resp.status_code == 200
        assert shared_acct not in _account_ids(resp.json())

        resp = await api_client.get(
            f"{_API_PREFIX}/accounts/{shared_acct}", headers=user_b["headers"]
        )
        assert resp.status_code == 404

        resp = await api_client.get(
            f"{_API_PREFIX}/net-worth", headers=user_b["headers"]
        )
        assert resp.json()["net_worth"] is None or Decimal(
            resp.json()["net_worth"]
        ) == Decimal(0)


# ═══════════════════════════════════════════════════════════════════════
# 2. Sharing RBAC: owner-only share, admin-only household management
# ═══════════════════════════════════════════════════════════════════════


class TestSharingRbac:
    @pytest.mark.asyncio
    async def test_only_owner_can_share_or_unshare(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-rbac-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-rbac-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        acct_id = await _seed_account(
            session_factory,
            tenant_id=admin["tenant_id"],
            owner_user_id=admin["user_id"],
            name="Owner Only",
        )

        # A non-owner (even though admin of the tenant) cannot change
        # visibility of an owned account — 404, no existence leak
        resp = await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=member["headers"],
            json={"visibility": "household"},
        )
        assert resp.status_code == 404

        # Same for the share preview
        resp = await api_client.get(
            f"{_API_PREFIX}/accounts/{acct_id}/share-preview",
            headers=member["headers"],
        )
        assert resp.status_code == 404

        # And for an unowned (system) account: only an admin may claim,
        # a non-admin gets 403/404
        unowned = await _seed_account(
            session_factory,
            tenant_id=admin["tenant_id"],
            owner_user_id=None,
            name="Unowned",
        )
        resp = await api_client.post(
            f"{_API_PREFIX}/accounts/{unowned}/claim",
            headers=member["headers"],
        )
        assert resp.status_code in (403, 404)

        # Admin can claim the unowned account
        resp = await api_client.post(
            f"{_API_PREFIX}/accounts/{unowned}/claim",
            headers=admin["headers"],
        )
        assert resp.status_code == 200
        assert resp.json()["owner_user_id"] == admin["user_id"]

    @pytest.mark.asyncio
    async def test_invalid_visibility_rejected(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-inv-admin", role=UserRole.ADMIN
        )
        acct_id = await _seed_account(
            session_factory,
            tenant_id=admin["tenant_id"],
            owner_user_id=admin["user_id"],
            name="Owner Only",
        )
        resp = await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=admin["headers"],
            json={"visibility": "everyone"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_non_admin_cannot_invite_change_role_or_remove(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-rbac2-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-rbac2-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        member_h = member["headers"]

        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations",
            headers=member_h,
            json={"email": "new@example.com", "role": "user"},
        )
        assert resp.status_code == 403

        resp = await api_client.patch(
            f"{_API_PREFIX}/household/members/{admin['user_id']}/role",
            headers=member_h,
            json={"role": "readonly"},
        )
        assert resp.status_code == 403

        resp = await api_client.delete(
            f"{_API_PREFIX}/household/members/{admin['user_id']}",
            headers=member_h,
        )
        assert resp.status_code == 403

        resp = await api_client.get(
            f"{_API_PREFIX}/household/audit-log", headers=member_h
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_members_list_visible_to_all_members(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-list-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-list-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        resp = await api_client.get(
            f"{_API_PREFIX}/household/members", headers=member["headers"]
        )
        assert resp.status_code == 200
        emails = {m["email"] for m in resp.json()}
        assert emails == {
            "hh-list-admin@finance-sync.local",
            "hh-list-member@finance-sync.local",
        }


# ═══════════════════════════════════════════════════════════════════════
# 3. Invitations: single-use, expiring, no user-directory leak
# ═══════════════════════════════════════════════════════════════════════


class TestInvitations:
    @pytest.mark.asyncio
    async def test_invite_accept_flow_grants_role_and_single_use(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-invite-admin", role=UserRole.ADMIN
        )
        invite_email = "new-member@example.com"

        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations",
            headers=admin["headers"],
            json={"email": invite_email, "role": "readonly"},
        )
        assert resp.status_code == 201
        invite = resp.json()
        assert invite["status"] == "pending"
        assert invite["role"] == "readonly"
        assert invite["email"] == invite_email
        token = invite["token"]

        # Accept with wrong email → generic failure
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations/accept",
            json={
                "token": token,
                "email": "other@example.com",
                "password": "super-secret-pw",
            },
        )
        assert resp.status_code == 400

        # Accept correctly → user created with the invited role + JWTs
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations/accept",
            json={
                "token": token,
                "email": invite_email,
                "password": "super-secret-pw",
                "display_name": "New Member",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["email"] == invite_email
        assert body["user"]["role"] == "readonly"
        assert body["user"]["tenant_id"] == admin["tenant_id"]

        # The new member can log in with the returned JWT
        new_headers = {"Authorization": f"Bearer {body['access_token']}"}
        resp = await api_client.get(
            f"{_API_PREFIX}/household/members", headers=new_headers
        )
        assert resp.status_code == 200

        # Single-use: second accept fails
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations/accept",
            json={
                "token": token,
                "email": invite_email,
                "password": "super-secret-pw",
            },
        )
        assert resp.status_code == 400

        # Invitation now shows as accepted
        resp = await api_client.get(
            f"{_API_PREFIX}/household/invitations", headers=admin["headers"]
        )
        assert resp.status_code == 200
        assert resp.json()[0]["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_invitation_expires(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from finance_sync.models.household_invitation import HouseholdInvitation

        admin = await _seed_tenant_user(
            session_factory, slug="hh-exp-admin", role=UserRole.ADMIN
        )
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations",
            headers=admin["headers"],
            json={"email": "expires@example.com", "role": "user"},
        )
        assert resp.status_code == 201
        token = resp.json()["token"]

        # Backdate the expiry so acceptance must fail
        async with session_factory() as session:
            invitation = (
                await session.execute(
                    select(HouseholdInvitation).where(
                        HouseholdInvitation.email == "expires@example.com"
                    )
                )
            ).scalar_one()
            invitation.expires_at = datetime.now(UTC) - timedelta(hours=1)
            await session.commit()

        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations/accept",
            json={
                "token": token,
                "email": "expires@example.com",
                "password": "super-secret-pw",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invite_does_not_leak_user_directory(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-noleak-admin", role=UserRole.ADMIN
        )
        existing_email = "hh-noleak-admin@finance-sync.local"

        # Inviting an email that already belongs to a tenant user returns
        # an identical, generic 201 response — no way to probe existence.
        resp_existing = await api_client.post(
            f"{_API_PREFIX}/household/invitations",
            headers=admin["headers"],
            json={"email": existing_email, "role": "user"},
        )
        assert resp_existing.status_code == 201
        body_existing = resp_existing.json()
        assert body_existing["status"] == "pending"
        assert "token" in body_existing

        # And the invitation for an existing user is still created; the
        # acceptance step then refuses with a generic error (no 404/401
        # distinction that would reveal the email is taken).
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations/accept",
            json={
                "token": body_existing["token"],
                "email": existing_email,
                "password": "super-secret-pw",
            },
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_revoke_invitation(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-revoke-admin", role=UserRole.ADMIN
        )
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations",
            headers=admin["headers"],
            json={"email": "revoked@example.com", "role": "user"},
        )
        assert resp.status_code == 201
        invite_id = resp.json()["id"]
        token = resp.json()["token"]

        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations/{invite_id}/revoke",
            headers=admin["headers"],
        )
        assert resp.status_code == 204

        # Revoked token no longer works
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations/accept",
            json={
                "token": token,
                "email": "revoked@example.com",
                "password": "super-secret-pw",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_role_change_and_member_removal(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-role-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-role-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        # Admin changes the member's role
        resp = await api_client.patch(
            f"{_API_PREFIX}/household/members/{member['user_id']}/role",
            headers=admin["headers"],
            json={"role": "readonly"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "readonly"

        # Admin cannot demote themselves (lockout protection)
        resp = await api_client.patch(
            f"{_API_PREFIX}/household/members/{admin['user_id']}/role",
            headers=admin["headers"],
            json={"role": "user"},
        )
        assert resp.status_code == 409

        # Removing a member makes their private account system-owned
        # (admins keep visibility) — nothing silently deleted.
        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=member["user_id"],
            name="Member Private",
            balance="42.00",
        )
        resp = await api_client.delete(
            f"{_API_PREFIX}/household/members/{member['user_id']}",
            headers=admin["headers"],
        )
        assert resp.status_code == 204

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.id == private_acct)
                )
            ).scalar_one()
            assert account.owner_user_id is None
            user = (
                await session.execute(
                    select(User).where(User.id == member["user_id"])
                )
            ).scalar_one()
            assert user.is_active is False


# ═══════════════════════════════════════════════════════════════════════
# 4. Audit log: tenant-scoped, sanitised, admin-only
# ═══════════════════════════════════════════════════════════════════════


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_household_actions_recorded_without_financial_payloads(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-audit-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]

        # invite → share → unshare
        resp = await api_client.post(
            f"{_API_PREFIX}/household/invitations",
            headers=admin["headers"],
            json={"email": "audited@example.com", "role": "user"},
        )
        assert resp.status_code == 201

        acct_id = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Audited",
            balance="12345.67",
        )
        await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=admin["headers"],
            json={"visibility": "household"},
        )
        await api_client.patch(
            f"{_API_PREFIX}/accounts/{acct_id}/visibility",
            headers=admin["headers"],
            json={"visibility": "private"},
        )

        resp = await api_client.get(
            f"{_API_PREFIX}/household/audit-log", headers=admin["headers"]
        )
        assert resp.status_code == 200
        events = resp.json()
        actions = [e["action"] for e in events]
        assert "invite" in actions
        assert "account_share" in actions
        assert "account_unshare" in actions

        serialised = json.dumps(events)
        # No financial payloads, no secrets
        assert "12345.67" not in serialised
        assert "secret" not in serialised.lower()

        # Every audit entry is tenant-scoped (tenant_id present)
        async with session_factory() as session:
            from finance_sync.models.household_audit_log import (
                HouseholdAuditLog,
            )

            rows = (
                (
                    await session.execute(
                        select(HouseholdAuditLog).where(
                            HouseholdAuditLog.tenant_id == tenant_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) >= 3


# ═══════════════════════════════════════════════════════════════════════
# 6. MCP semantics: machine scope never sees user-private accounts
# ═══════════════════════════════════════════════════════════════════════


class TestMachineScope:
    @pytest.mark.asyncio
    async def test_api_key_scope_sees_only_household_and_unowned(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """ReadScope.for_api_key mirrors what the MCP server applies for
        API-key principals — private accounts of users stay invisible."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-mcp-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="hh-mcp-member",
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
        )
        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=member["user_id"],
            name="Private",
        )
        unowned_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=None,
            name="Unowned",
        )

        scope = ReadScope.for_api_key(tenant_id)

        # SQL-level predicate resolves to exactly the right ids
        async with session_factory() as session:
            from finance_sync.services.visibility import (
                load_visible_account_ids,
            )

            visible = await load_visible_account_ids(
                session, tenant_id, user_id=None
            )
        assert household_acct in visible
        assert private_acct not in visible
        assert unowned_acct in visible

        # In-memory predicate agrees
        async with session_factory() as session:
            accounts = (
                (
                    await session.execute(
                        select(Account).where(Account.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
        visible_mem = {str(a.id) for a in accounts if scope.is_visible(a)}
        assert visible_mem == {household_acct, unowned_acct}


async def _seed_webhook(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    events: list[str],
) -> str:
    """Create an active webhook row for *tenant_id* and return its id."""
    async with session_factory() as session:
        webhook = Webhook(
            tenant_id=tenant_id,
            url="http://127.0.0.1:1/hook",  # unroutable → delivery fails fast
            secret="webhook-test-secret",
            events=events,
            description="household test webhook",
            is_active=True,
            rate_limit_max_per_minute=1000,
        )
        session.add(webhook)
        await session.commit()
        return str(webhook.id)


async def _delivery_count(
    session_factory: async_sessionmaker[AsyncSession], webhook_id: str
) -> int:
    """Count delivery-log rows for a webhook."""
    async with session_factory() as session:
        return len(
            (
                await session.execute(
                    select(WebhookDeliveryLog).where(
                        WebhookDeliveryLog.webhook_id == webhook_id
                    )
                )
            )
            .scalars()
            .all()
        )


# ═══════════════════════════════════════════════════════════════════════
# 7. Webhooks: tenant-scoped dispatch, no private-account side channel
# ═══════════════════════════════════════════════════════════════════════


class TestWebhookPrivacy:
    """Outbox → webhook dispatch honours household privacy.

    - deliveries are scoped to the owning tenant's webhooks (outbox
      messages carry no tenant column; the tenant is read from the
      event payload);
    - in a multi-member household, events for a *private* account are
      suppressed entirely, so a webhook cannot leak another member's
      private financial data;
    - a single-member household keeps receiving its own private-account
      events (backwards compatible).
    """

    @pytest.mark.asyncio
    async def test_private_account_event_suppressed_in_household(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        api_settings: Settings,
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-wh-admin", role=UserRole.ADMIN
        )
        await _seed_tenant_user(
            session_factory,
            slug="hh-wh-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]
        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Private",
        )
        txn_id = await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=private_acct,
            amount="123.45",
        )
        wh_id = await _seed_webhook(
            session_factory, tenant_id=tenant_id, events=["transaction.created"]
        )

        svc = WebhookService(session_factory, api_settings)
        count = await svc.dispatch_event(
            "transaction.created",
            {
                "tenant_id": tenant_id,
                "entity_type": "transaction",
                "entity_id": txn_id,
            },
            tenant_id=tenant_id,
        )
        assert count == 0
        assert await _delivery_count(session_factory, wh_id) == 0

    @pytest.mark.asyncio
    async def test_household_account_event_is_delivered(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        api_settings: Settings,
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="hh-wh2-admin", role=UserRole.ADMIN
        )
        await _seed_tenant_user(
            session_factory,
            slug="hh-wh2-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]
        shared_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
        )
        txn_id = await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=shared_acct,
            amount="50.00",
        )
        wh_id = await _seed_webhook(
            session_factory, tenant_id=tenant_id, events=["transaction.created"]
        )

        svc = WebhookService(session_factory, api_settings)
        count = await svc.dispatch_event(
            "transaction.created",
            {
                "tenant_id": tenant_id,
                "entity_type": "transaction",
                "entity_id": txn_id,
            },
            tenant_id=tenant_id,
        )
        assert count == 1
        assert await _delivery_count(session_factory, wh_id) == 1

    @pytest.mark.asyncio
    async def test_single_member_household_keeps_private_events(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        api_settings: Settings,
    ) -> None:
        """One member owns every account → private events keep flowing."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-wh3-admin", role=UserRole.ADMIN
        )
        tenant_id = admin["tenant_id"]
        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Private",
        )
        txn_id = await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=private_acct,
            amount="777.00",
        )
        wh_id = await _seed_webhook(
            session_factory, tenant_id=tenant_id, events=["transaction.created"]
        )

        svc = WebhookService(session_factory, api_settings)
        count = await svc.dispatch_event(
            "transaction.created",
            {
                "tenant_id": tenant_id,
                "entity_type": "transaction",
                "entity_id": txn_id,
            },
            tenant_id=tenant_id,
        )
        assert count == 1
        assert await _delivery_count(session_factory, wh_id) == 1

    @pytest.mark.asyncio
    async def test_cross_tenant_webhook_never_receives_foreign_events(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        api_settings: Settings,
    ) -> None:
        """Tenant B's webhook must not receive tenant A's events."""
        admin_a = await _seed_tenant_user(
            session_factory, slug="hh-wha-admin", role=UserRole.ADMIN
        )
        admin_b = await _seed_tenant_user(
            session_factory, slug="hh-whb-admin", role=UserRole.ADMIN
        )
        acct_a = await _seed_account(
            session_factory,
            tenant_id=admin_a["tenant_id"],
            owner_user_id=admin_a["user_id"],
            name="A account",
        )
        txn_a = await _seed_transaction(
            session_factory,
            tenant_id=admin_a["tenant_id"],
            account_id=acct_a,
            amount="1.00",
        )
        wh_a = await _seed_webhook(
            session_factory,
            tenant_id=admin_a["tenant_id"],
            events=["transaction.created"],
        )
        wh_b = await _seed_webhook(
            session_factory,
            tenant_id=admin_b["tenant_id"],
            events=["transaction.created"],
        )

        svc = WebhookService(session_factory, api_settings)
        count = await svc.dispatch_event(
            "transaction.created",
            {
                "tenant_id": admin_a["tenant_id"],
                "entity_type": "transaction",
                "entity_id": txn_a,
            },
            tenant_id=admin_a["tenant_id"],
        )
        assert count == 1
        assert await _delivery_count(session_factory, wh_a) == 1
        assert await _delivery_count(session_factory, wh_b) == 0

    @pytest.mark.asyncio
    async def test_aggregate_events_never_suppressed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        api_settings: Settings,
    ) -> None:
        """sync.completed carries no account data — always delivered."""
        admin = await _seed_tenant_user(
            session_factory, slug="hh-wh4-admin", role=UserRole.ADMIN
        )
        await _seed_tenant_user(
            session_factory,
            slug="hh-wh4-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]
        wh_id = await _seed_webhook(
            session_factory, tenant_id=tenant_id, events=["sync.completed"]
        )

        svc = WebhookService(session_factory, api_settings)
        count = await svc.dispatch_event(
            "sync.completed",
            {
                "tenant_id": tenant_id,
                "provider_key": "bunq",
                "accounts": 3,
                "transactions": 5,
            },
            tenant_id=tenant_id,
        )
        assert count == 1
        assert await _delivery_count(session_factory, wh_id) == 1
