"""PG integration: household side-channel leak prevention.

Scope: kanban task t_fd39a50e — the acceptance criteria require that no
read surface leaks another household member's private account data.
``test_household_sharing_pg.py`` proves the core read APIs (accounts,
transactions, holdings, net worth, cashflow, dividends, portfolio,
allocation, performance) honour private-by-default.  This file closes
the remaining surfaces that are derived from accounts:

- ``/subscriptions`` (list, single, detect) — a subscription detected
  from *my* private account must not be visible to another member;
- ``/tax-lots`` (list + summary) — cost-basis data of a private account
  must not be visible to another member;
- ``/reconciliation`` runs + findings — findings referencing another
  member's private account must not be visible;
- ``/scheduled-payments`` and ``/card-transactions`` — derived rows of a
  private account must not leak (regression guard for ReadService
  scoping);
- AI summary data gathering — the prompt context must only contain the
  principal's visible accounts.

Each test seeds a two-member household (admin + user) where the admin
owns a *private* account with derived rows and a *shared* account, then
asserts the user sees only the shared data.
"""

from __future__ import annotations

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
from finance_sync.models import Account, Tenant, Transaction, User
from finance_sync.models.card_transaction import CardTransaction
from finance_sync.models.detected_subscription import DetectedSubscription
from finance_sync.models.enums import (
    AccountVisibility,
    CardAuthorizationType,
    DetectionMethod,
    ReconciliationResultKind,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    ScheduleFrequency,
    SubscriptionConfidence,
    SubscriptionStatus,
    TransactionStatus,
    TransactionType,
    UserRole,
)
from finance_sync.models.reconciliation import (
    ReconciliationResult,
    ReconciliationRun,
)
from finance_sync.models.scheduled_payment import ScheduledPayment
from finance_sync.models.security import Security
from finance_sync.models.tax_lot import TaxLot
from finance_sync.services.auth import create_access_token, hash_password

pytestmark = pytest.mark.integration

_INT_SECRET = "household-int-secret-key-16chars"
_INT_MASTER_KEY = "cd" * 32  # 64 hex chars → 32-byte AES-256 key

_API_PREFIX = "/api/v1"


# ── App fixtures (same wiring as test_household_sharing_pg) ──────────


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
    tenant: Tenant | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Persist a tenant (unless given) + user and return a signed JWT."""
    email = f"{slug}@finance-sync.local"
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
    description: str = "txn",
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
            description=description,
        )
        session.add(txn)
        await session.commit()
        return str(txn.id)


async def _seed_security(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a security row (holding/tax-lot FK target) and return its id."""
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


async def _seed_subscription(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    account_id: str,
    merchant: str,
    amount: str = "-9.99",
) -> str:
    """Create a detected-subscription row and return its id."""
    async with session_factory() as session:
        now = datetime.now(UTC)
        sub = DetectedSubscription(
            tenant_id=tenant_id,
            merchant_name=merchant,
            raw_description=f"charge from {merchant}",
            amount=Decimal(amount),
            currency_code="EUR",
            frequency_days=30,
            frequency_label="monthly",
            confidence=SubscriptionConfidence.HIGH,
            detection_method=DetectionMethod.EXACT_AMOUNT,
            status=SubscriptionStatus.ACTIVE,
            account_id=account_id,
            provider_key="bunq",
            first_detected_at=now - timedelta(days=60),
            last_detected_at=now - timedelta(days=1),
            occurrence_count=3,
        )
        session.add(sub)
        await session.commit()
        return str(sub.id)


async def _seed_tax_lot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    account_id: str,
    security_id: str,
    acquired_at: datetime | None = None,
) -> str:
    """Create a tax-lot row and return its id."""
    async with session_factory() as session:
        lot = TaxLot(
            tenant_id=tenant_id,
            account_id=account_id,
            security_id=security_id,
            quantity=Decimal(10),
            remaining_quantity=Decimal(10),
            cost_basis_total=Decimal("500.00"),
            cost_basis_per_unit=Decimal("50.00"),
            currency_code="EUR",
            acquired_at=acquired_at or (datetime.now(UTC) - timedelta(days=30)),
        )
        session.add(lot)
        await session.commit()
        return str(lot.id)


async def _seed_reconciliation_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    private_account_id: str,
) -> tuple[str, str]:
    """Create a completed reconciliation run + one finding referencing
    the private account; return (run_id, finding_id)."""
    async with session_factory() as session:
        run = ReconciliationRun(
            tenant_id=tenant_id,
            status=ReconciliationRunStatus.COMPLETED,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            completed_at=datetime.now(UTC),
            scope={"date_from": "2026-01-01", "date_to": "2026-07-01"},
            finding_count=1,
            summary={
                "by_kind": {"duplicate_transaction": 1},
                "by_severity": {"info": 1},
            },
        )
        session.add(run)
        await session.flush()
        finding = ReconciliationResult(
            run_id=str(run.id),
            tenant_id=tenant_id,
            kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
            severity=ReconciliationSeverity.INFO,
            account_id=private_account_id,
            provider_key="bunq",
            amount=Decimal("123.45"),
            description="Duplicate transaction on private account",
        )
        session.add(finding)
        await session.commit()
        return str(run.id), str(finding.id)


async def _seed_scheduled_payment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    account_id: str,
    amount: str = "-25.00",
) -> str:
    """Create a scheduled-payment row and return its id."""
    async with session_factory() as session:
        sp = ScheduledPayment(
            tenant_id=tenant_id,
            provider_key="bunq",
            connection_id=None,
            external_schedule_id=str(uuid4()),
            account_id=account_id,
            amount=Decimal(amount),
            currency_code="EUR",
            amount_in_base=Decimal(amount),
            frequency=ScheduleFrequency.MONTHLY,
            next_execution_date=datetime.now(UTC) + timedelta(days=7),
            description="scheduled payment",
        )
        session.add(sp)
        await session.commit()
        return str(sp.id)


async def _seed_card_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    account_id: str,
    amount: str = "-33.00",
    merchant: str = "Card Merchant",
) -> str:
    """Create a card-transaction row and return its id."""
    async with session_factory() as session:
        ct = CardTransaction(
            tenant_id=tenant_id,
            provider_key="bunq",
            connection_id=None,
            external_card_transaction_id=str(uuid4()),
            account_id=account_id,
            amount=Decimal(amount),
            amount_in_base=Decimal(amount),
            currency_code="EUR",
            merchant_name=merchant,
            occurred_at=datetime.now(UTC) - timedelta(days=1),
            transaction_type=TransactionType.PAYMENT,
            authorization_type=CardAuthorizationType.SETTLEMENT,
            status=TransactionStatus.BOOKED,
        )
        session.add(ct)
        await session.commit()
        return str(ct.id)


def _item_ids(payload: dict[str, Any], key: str = "id") -> set[str]:
    """Extract ids from a list or {items: [...]} payload."""
    if isinstance(payload, list):
        return {item[key] for item in payload}
    return {item[key] for item in payload.get("items", [])}


# ═══════════════════════════════════════════════════════════════════════
# 1. Subscriptions: private-account subscriptions never leak
# ═══════════════════════════════════════════════════════════════════════


class TestSubscriptionPrivacy:
    """Subscriptions require ``subscriptions:read`` (admin-only), so the
    realistic two-user leak scenario is admin ↔ admin: a second admin
    household member must never see subscriptions detected from the
    first admin's *private* accounts."""

    @pytest.mark.asyncio
    async def test_member_never_sees_private_account_subscriptions(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="sc-sub-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="sc-sub-member",
            role=UserRole.ADMIN,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner Private",
        )
        shared_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
        )
        private_sub = await _seed_subscription(
            session_factory,
            tenant_id=tenant_id,
            account_id=private_acct,
            merchant="Netflix Private",
        )
        shared_sub = await _seed_subscription(
            session_factory,
            tenant_id=tenant_id,
            account_id=shared_acct,
            merchant="Spotify Shared",
        )

        member_h = member["headers"]

        # List: only the shared subscription is visible
        resp = await api_client.get(
            f"{_API_PREFIX}/subscriptions", headers=member_h
        )
        assert resp.status_code == 200
        ids = _item_ids(resp.json())
        assert private_sub not in ids
        assert shared_sub in ids

        # Single read of the private-account subscription → 404
        resp = await api_client.get(
            f"{_API_PREFIX}/subscriptions/{private_sub}", headers=member_h
        )
        assert resp.status_code == 404

        # Single read of the shared subscription → 200
        resp = await api_client.get(
            f"{_API_PREFIX}/subscriptions/{shared_sub}", headers=member_h
        )
        assert resp.status_code == 200

        # Owner still sees both (sanity — data exists)
        resp = await api_client.get(
            f"{_API_PREFIX}/subscriptions", headers=admin["headers"]
        )
        ids = _item_ids(resp.json())
        assert private_sub in ids
        assert shared_sub in ids

    @pytest.mark.asyncio
    async def test_detection_never_reads_private_account_transactions(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="sc-det-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="sc-det-member",
            role=UserRole.ADMIN,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner Private",
        )
        # A recurring outgoing payment on the *private* account
        for i in range(3):
            await _seed_transaction(
                session_factory,
                tenant_id=tenant_id,
                account_id=private_acct,
                amount="-12.50",
                description=f"Secret Gym {i}",
            )

        # Detection run as the member must not surface the private rows
        resp = await api_client.get(
            f"{_API_PREFIX}/subscriptions/detected",
            headers=member["headers"],
            params={"min_occurrences": 2},
        )
        assert resp.status_code == 200
        results = resp.json()
        descriptions = {r.get("merchant_name") or "" for r in results}
        assert "Secret Gym" not in descriptions
        assert results == []


# ═══════════════════════════════════════════════════════════════════════
# 2. Tax lots: cost basis of private accounts never leaks
# ═══════════════════════════════════════════════════════════════════════


class TestTaxLotPrivacy:
    @pytest.mark.asyncio
    async def test_member_never_sees_private_account_tax_lots(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="sc-tl-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="sc-tl-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner Private",
            account_type="investment",
        )
        shared_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
            account_type="investment",
        )
        sec_id = await _seed_security(session_factory)
        private_lot = await _seed_tax_lot(
            session_factory,
            tenant_id=tenant_id,
            account_id=private_acct,
            security_id=sec_id,
        )
        shared_lot = await _seed_tax_lot(
            session_factory,
            tenant_id=tenant_id,
            account_id=shared_acct,
            security_id=sec_id,
        )

        member_h = member["headers"]

        resp = await api_client.get(f"{_API_PREFIX}/tax-lots", headers=member_h)
        assert resp.status_code == 200
        ids = _item_ids(resp.json())
        assert private_lot not in ids
        assert shared_lot in ids

        # Explicit filter by the private account must yield nothing
        resp = await api_client.get(
            f"{_API_PREFIX}/tax-lots",
            headers=member_h,
            params={"account_id": private_acct},
        )
        assert resp.status_code == 200
        assert _item_ids(resp.json()) == set()

        # Summary must not include the private account's cost basis
        resp = await api_client.get(
            f"{_API_PREFIX}/tax-lots/summary", headers=member_h
        )
        assert resp.status_code == 200
        body = resp.json()
        serialised = str(body)
        assert "500.00" not in serialised or Decimal(
            str(body.get("total_cost_basis", 0))
        ) == Decimal(0)


# ═══════════════════════════════════════════════════════════════════════
# 3. Reconciliation: findings on private accounts never leak
# ═══════════════════════════════════════════════════════════════════════


class TestReconciliationPrivacy:
    """Reconciliation requires ``reconciliation:read`` (admin-only), so
    the two-user scenario is admin ↔ admin: a second admin must never
    see findings referencing the first admin's private accounts."""

    @pytest.mark.asyncio
    async def test_member_never_sees_private_account_findings(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="sc-rec-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="sc-rec-member",
            role=UserRole.ADMIN,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner Private",
        )
        run_id, finding_id = await _seed_reconciliation_run(
            session_factory,
            tenant_id=tenant_id,
            private_account_id=private_acct,
        )

        member_h = member["headers"]

        # Run detail: findings referencing the private account are hidden
        resp = await api_client.get(
            f"{_API_PREFIX}/reconciliation/{run_id}", headers=member_h
        )
        assert resp.status_code == 200
        body = resp.json()
        result_ids = {r["id"] for r in body["results"]}
        assert finding_id not in result_ids
        assert body["total_results"] == 0


# ═══════════════════════════════════════════════════════════════════════
# 4. Derived read APIs already scoped (regression guards)
# ═══════════════════════════════════════════════════════════════════════


class TestDerivedReadApiScoping:
    @pytest.mark.asyncio
    async def test_scheduled_payments_scoped_to_visible_accounts(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="sc-sp-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="sc-sp-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner Private",
        )
        shared_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
        )
        private_sp = await _seed_scheduled_payment(
            session_factory, tenant_id=tenant_id, account_id=private_acct
        )
        shared_sp = await _seed_scheduled_payment(
            session_factory, tenant_id=tenant_id, account_id=shared_acct
        )

        resp = await api_client.get(
            f"{_API_PREFIX}/scheduled-payments", headers=member["headers"]
        )
        assert resp.status_code == 200
        ids = _item_ids(resp.json())
        assert private_sp not in ids
        assert shared_sp in ids

    @pytest.mark.asyncio
    async def test_card_transactions_scoped_to_visible_accounts(
        self,
        api_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await _seed_tenant_user(
            session_factory, slug="sc-ct-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="sc-ct-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Owner Private",
        )
        shared_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared",
            visibility=AccountVisibility.HOUSEHOLD.value,
        )
        private_ct = await _seed_card_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=private_acct,
            merchant="Private Card Co",
        )
        shared_ct = await _seed_card_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=shared_acct,
            merchant="Shared Card Co",
        )

        resp = await api_client.get(
            f"{_API_PREFIX}/card-transactions", headers=member["headers"]
        )
        assert resp.status_code == 200
        ids = _item_ids(resp.json())
        assert private_ct not in ids
        assert shared_ct in ids


# ═══════════════════════════════════════════════════════════════════════
# 5. AI summary data gathering honours the principal's scope
# ═══════════════════════════════════════════════════════════════════════


class TestAiSummaryScoping:
    @pytest.mark.asyncio
    async def test_collect_financial_data_excludes_private_accounts(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from finance_sync.services.ai_summary import AISummaryService
        from finance_sync.services.visibility import ReadScope

        admin = await _seed_tenant_user(
            session_factory, slug="sc-ai-admin", role=UserRole.ADMIN
        )
        member = await _seed_tenant_user(
            session_factory,
            slug="sc-ai-member",
            role=UserRole.USER,
            tenant_id=admin["tenant_id"],
        )
        tenant_id = admin["tenant_id"]

        private_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Secret Private Account",
            balance="90000.00",
        )
        await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=private_acct,
            amount="-5000.00",
            description="private luxury purchase",
        )
        shared_acct = await _seed_account(
            session_factory,
            tenant_id=tenant_id,
            owner_user_id=admin["user_id"],
            name="Shared Household Account",
            balance="200.00",
            visibility=AccountVisibility.HOUSEHOLD.value,
        )
        await _seed_transaction(
            session_factory,
            tenant_id=tenant_id,
            account_id=shared_acct,
            amount="-25.00",
            description="groceries",
        )

        settings = Settings(
            secret_key=_INT_SECRET,  # pyright: ignore[reportArgumentType]
            ai_enabled=True,
            ai_provider="openai",
            ai_api_key="sk-test",  # pyright: ignore[reportArgumentType]
        )
        scope = ReadScope.for_user(
            await _load_user(session_factory, member["user_id"])
        )
        async with session_factory() as session:
            svc = AISummaryService(session, settings, scope=scope)
            data = await svc._collect_financial_data(  # type: ignore[attr-defined]
                tenant_id, time_period_days=30
            )

        serialised = str(data)
        # Private account name / balance / transactions never enter the prompt
        assert "Secret Private Account" not in serialised
        assert "private luxury purchase" not in serialised
        # Shared account data is present
        assert "Shared Household Account" in serialised
        assert "groceries" in serialised


async def _load_user(
    session_factory: async_sessionmaker[AsyncSession], user_id: str
) -> User:
    async with session_factory() as session:
        return (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
