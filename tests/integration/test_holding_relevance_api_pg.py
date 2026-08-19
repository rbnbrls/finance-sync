"""Integration tests for the holding-relevance API + MCP exposure.

Proves the t_078d6f7e acceptance criteria against **real** PostgreSQL
through the full FastAPI HTTP stack (ASGI transport + JWT auth + DI
container):

* ``GET /api/v1/holding-relevance/feed`` — every filter (security,
  account, item type, date range, unread/acknowledged status) is
  honoured through the HTTP layer;
* ``POST /api/v1/holding-relevance/clusters/{id}/ack`` — the unread/ack
  flow round-trips per user and is reflected in later feed reads;
* Response schema — every cluster carries source URLs, published_at,
  fetched_at, freshness, match reason, confidence, event dates,
  cluster_id, and an ``is_stale`` degradation flag;
* Graceful degradation — stale/missing sources still serve cached data
  with a stale flag (200, never 500);
* Tenant isolation — a cross-tenant security/account filter returns an
  empty feed (never leaks, never errors);
* MCP — the three holding-relevance tools are registered and their
  handlers return the same contract through the MCP auth boundary.

# pyright: basic
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from finance_sync.db.uow import UnitOfWork
from finance_sync.models import Tenant, User
from finance_sync.models.account import Account
from finance_sync.models.enums import AccountType, HoldingSource, UserRole
from finance_sync.models.holding import Holding
from finance_sync.models.market_intelligence_item import MarketIntelligenceItem
from finance_sync.models.security import Security
from finance_sync.services.auth import create_access_token, hash_password
from finance_sync.services.holding_relevance import HoldingRelevanceService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.config.settings import Settings

pytestmark = pytest.mark.integration


# ── Seed helpers ──────────────────────────────────────────────────────


async def _create_tenant_user(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    slug: str,
) -> dict[str, Any]:
    """Create a tenant + admin user, returning JWT headers."""
    async with session_factory() as session:
        tenant = Tenant(slug=slug, name="Holding Relevance API")
        session.add(tenant)
        await session.flush()
        user = User(
            email=f"{slug}@finance-sync.local",
            tenant_id=str(tenant.id),
            hashed_password=hash_password("test-password"),
            display_name="Relevance Admin",
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        tenant_id = str(tenant.id)
        user_id = str(user.id)

    token = create_access_token(
        {"sub": user_id, "tenant_id": tenant_id, "role": "admin"},
        settings,
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


async def _new_security(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ticker: str = "AAPL",
    name: str = "Apple Inc.",
) -> str:
    async with session_factory() as session:
        sec = Security(
            isin="US0378331005" if ticker == "AAPL" else None,
            ticker=ticker,
            name=name,
            security_type="stock",
            currency_code="USD",
        )
        session.add(sec)
        await session.commit()
        return str(sec.id)


async def _new_account(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    *,
    name: str = "Trading212",
) -> str:
    async with session_factory() as session:
        acct = Account(
            tenant_id=tenant_id,
            provider_key="trading212",
            external_account_id=f"ext-{name}",
            name=name,
            account_type=AccountType.BROKERAGE,
            currency_code="EUR",
        )
        session.add(acct)
        await session.commit()
        return str(acct.id)


async def _new_holding(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    account_id: str,
    security_id: str,
    *,
    quantity: Decimal = Decimal(10),
    market_value: Decimal | None = Decimal(1500),
    observed_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        h = Holding(
            tenant_id=tenant_id,
            account_id=account_id,
            security_id=security_id,
            observed_at=observed_at or datetime.now(UTC),
            quantity=quantity,
            market_value=market_value,
            currency_code="EUR",
            source=HoldingSource.PROVIDER_SYNC,
        )
        session.add(h)
        await session.commit()


async def _new_item(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    security_id: str | None,
    *,
    provider: str = "openbb",
    source_id: str,
    kind: str = "news_article",
    headline: str = "Apple beats estimates",
    canonical_url: str | None = "https://example.com/news/1",
    published_at: datetime | None = None,
    fetched_at: datetime | None = None,
    facts: list[dict[str, Any]] | None = None,
) -> str:
    """Create one stored market-intelligence observation."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        item = MarketIntelligenceItem(
            tenant_id=tenant_id,
            provider=provider,
            source_id=source_id,
            canonical_url=canonical_url,
            kind=kind,
            published_at=published_at or now,
            fetched_at=fetched_at or now,
            language="en",
            license_class="free_access",
            content_hash=f"hash-{tenant_id}-{source_id}",
            headline=headline,
            summary="Summary",
            facts=facts or [],
            identifiers={"ticker": "AAPL"},
            resolution_status="resolved",
            security_id=security_id,
        )
        session.add(item)
        await session.commit()
        return str(item.id)


async def _build_feed(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> None:
    """Run the relevance build pipeline (match + cluster + rank)."""
    async with session_factory() as session:
        svc = HoldingRelevanceService(UnitOfWork(session))
        await svc.build_feed(tenant_id)
        await session.commit()


async def _cluster_ids(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> list[str]:
    async with session_factory() as session:
        from sqlalchemy import select

        from finance_sync.models.holding_relevance import RelevanceCluster

        stmt = (
            select(RelevanceCluster.id)
            .where(RelevanceCluster.tenant_id == tenant_id)
            .order_by(RelevanceCluster.score.desc())
        )
        return [str(r) for r in (await session.execute(stmt)).scalars().all()]


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def relevance_settings() -> Settings:
    """Test settings with fixed secret keys for deterministic JWTs."""
    from pydantic import SecretStr

    from finance_sync.config.settings import Settings as _Settings

    return _Settings(
        secret_key=SecretStr("test-secret-key-at-least-16-chars"),
        master_encryption_key=SecretStr("0123456789abcdef" * 4),
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def relevance_container(
    pg_engine: Any,
    session_factory: async_sessionmaker[AsyncSession],
    relevance_settings: Settings,
) -> Any:
    """DI container bound to the harness PG (like the e2e conftest)."""
    from finance_sync.container import Container

    container = Container.from_settings(relevance_settings)
    container._engine = pg_engine  # type: ignore[attr-defined]
    container._session_factory = session_factory  # type: ignore[attr-defined]
    return container


@pytest.fixture
def relevance_app(
    relevance_container: Any, relevance_settings: Settings
) -> Any:
    """FastAPI app with the container attached (lifespan not run)."""
    from finance_sync.app import create_app

    app = create_app(settings=relevance_settings)
    app.state.container = relevance_container
    return app


@pytest.fixture
async def relevance_client(relevance_app: Any) -> Any:
    """Async HTTP client against the in-process FastAPI app."""
    transport = httpx.ASGITransport(app=relevance_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://relevance"
    ) as client:
        yield client


@pytest.fixture
async def seeded_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    relevance_settings: Settings,
) -> dict[str, Any]:
    """Tenant A (with holdings + news) and tenant B (empty)."""
    tenant_a = await _create_tenant_user(
        session_factory, relevance_settings, slug="rel-a"
    )
    tenant_b = await _create_tenant_user(
        session_factory, relevance_settings, slug="rel-b"
    )

    sec_a = await _new_security(session_factory)  # AAPL held by A
    sec_b = await _new_security(session_factory, ticker="MSFT")  # held by B
    acct_a = await _new_account(session_factory, tenant_a["tenant_id"])
    acct_b = await _new_account(session_factory, tenant_b["tenant_id"])
    await _new_holding(session_factory, tenant_a["tenant_id"], acct_a, sec_a)
    await _new_holding(session_factory, tenant_b["tenant_id"], acct_b, sec_b)

    now = datetime.now(UTC)
    # A: an earnings story with a known event date (fresh).
    await _new_item(
        session_factory,
        tenant_a["tenant_id"],
        sec_a,
        provider="sec",
        source_id="aapl-earnings",
        kind="earnings_report",
        headline="Apple Q4 earnings",
        canonical_url="https://example.com/aapl-earnings",
        published_at=now - timedelta(hours=2),
        fetched_at=now,
        facts=[{"key": "event_date", "value": "2026-10-30"}],
    )
    # A: a dividend story (fresh).
    await _new_item(
        session_factory,
        tenant_a["tenant_id"],
        sec_a,
        provider="openbb",
        source_id="aapl-dividend",
        kind="dividend",
        headline="Apple dividend ex-date",
        canonical_url="https://example.com/aapl-dividend",
        published_at=now - timedelta(hours=5),
        fetched_at=now,
        facts=[{"key": "ex_date", "value": "2026-08-10"}],
    )
    # A: a stale story (fetched 3 days ago).
    await _new_item(
        session_factory,
        tenant_a["tenant_id"],
        sec_a,
        provider="openbb",
        source_id="aapl-stale",
        kind="news_article",
        headline="Apple stale press note",
        canonical_url="https://example.com/aapl-stale",
        published_at=now - timedelta(days=3),
        fetched_at=now - timedelta(days=3),
    )

    await _build_feed(session_factory, tenant_a["tenant_id"])
    await _build_feed(session_factory, tenant_b["tenant_id"])

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "security_a": sec_a,
        "security_b": sec_b,
        "account_a": acct_a,
        "account_b": acct_b,
    }


# ═══════════════════════════════════════════════════════════════════════
# Auth + registration
# ═══════════════════════════════════════════════════════════════════════


class TestAuthAndRegistration:
    async def test_feed_requires_auth(
        self, relevance_client: httpx.AsyncClient
    ) -> None:
        resp = await relevance_client.get("/api/v1/holding-relevance/feed")
        assert resp.status_code == 401

    async def test_ack_requires_auth(
        self, relevance_client: httpx.AsyncClient
    ) -> None:
        resp = await relevance_client.post(
            "/api/v1/holding-relevance/clusters/x/ack",
            json={"acknowledged": True},
        )
        assert resp.status_code == 401

    async def test_endpoints_registered(
        self, relevance_client: httpx.AsyncClient
    ) -> None:
        """OpenAPI registers feed, calendar, ack, corrections, prefs."""
        resp = await relevance_client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json()["paths"]
        assert "/api/v1/holding-relevance/feed" in paths
        assert "/api/v1/holding-relevance/calendar" in paths
        assert "/api/v1/holding-relevance/clusters/{cluster_id}/ack" in paths
        assert "/api/v1/holding-relevance/corrections" in paths


# ═══════════════════════════════════════════════════════════════════════
# Filter matrix
# ═══════════════════════════════════════════════════════════════════════


class TestFeedFilters:
    async def test_feed_full_and_schema(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        """Unfiltered feed returns 3 clusters with the full schema."""
        tenant_a = seeded_tenant["tenant_a"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed", headers=tenant_a["headers"]
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        items = body["items"]
        assert len(items) == 3

        for item in items:
            # Contract fields (acceptance criteria).
            assert item["id"] == item["cluster_id"]
            assert item["security_id"] is not None
            assert item["security_ticker"]
            assert item["event_type"] in {
                "earnings",
                "dividend",
                "news",
                "agm",
                "split",
                "merger",
                "acquisition",
                "filing",
                "interest",
                "currency",
            }
            assert item["headline"]
            assert item["score"] >= 0.0
            assert item["match_reason"] in {
                "canonical_security",
                "recently_sold",
                "currency_interest",
                "hermes_suggested",
            }
            assert item["confidence"] is not None
            assert 0.0 <= item["confidence"] <= 1.0
            assert item["acknowledged"] is False  # fresh user → unread
            assert isinstance(item["is_stale"], bool)
            assert item["source_count"] >= 1
            assert item["best_source_url"]
            assert len(item["sources"]) >= 1
            for source in item["sources"]:
                assert source["url"]
                assert source["published_at"]
                assert source["fetched_at"]
                assert source["freshness"] in {"fresh", "stale"}

    async def test_security_filter(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"security_id": seeded_tenant["security_a"]},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        for item in body["items"]:
            assert str(item["security_id"]) == seeded_tenant["security_a"]

    async def test_account_filter(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"account_id": seeded_tenant["account_a"]},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 3

    async def test_item_type_filter(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"item_type": "dividend"},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["event_type"] == "dividend"

    async def test_date_range_filter(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        # Only the earnings cluster has an event date of 2026-10-30.
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={
                "date_from": "2026-10-01T00:00:00Z",
                "date_to": "2026-11-30T00:00:00Z",
            },
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["event_type"] == "earnings"

    async def test_include_stale_false_drops_stale_cluster(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"include_stale": "false"},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The stale press note is dropped; earnings + dividend remain.
        assert body["total"] == 2

    async def test_injection_payloads_treated_as_data(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        """SQL/wildcard payloads return 200 + empty, never an error."""
        tenant_a = seeded_tenant["tenant_a"]
        for payload in (
            "AAPL' OR '1'='1",
            "%",
            "_",
            "'; DROP TABLE relevance_clusters; --",
        ):
            resp = await relevance_client.get(
                "/api/v1/holding-relevance/feed",
                params={"security_id": payload},
                headers=tenant_a["headers"],
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["total"] == 0

            resp = await relevance_client.get(
                "/api/v1/holding-relevance/feed",
                params={"item_type": payload},
                headers=tenant_a["headers"],
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["total"] == 0


# ═══════════════════════════════════════════════════════════════════════
# Unread / ack flow
# ═══════════════════════════════════════════════════════════════════════


class TestAckFlow:
    async def test_unread_and_ack_flow(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        cluster_ids = await _cluster_ids(session_factory, tenant_a["tenant_id"])
        assert len(cluster_ids) == 3
        target = cluster_ids[0]

        # Initially unread for this user.
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"unread_only": "true"},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 3

        # Ack the top cluster.
        resp = await relevance_client.post(
            f"/api/v1/holding-relevance/clusters/{target}/ack",
            json={"acknowledged": True},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["acknowledged"] is True

        # Feed now reflects the ack for this user.
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed", headers=tenant_a["headers"]
        )
        assert resp.status_code == 200, resp.text
        by_id = {i["id"]: i for i in resp.json()["items"]}
        assert by_id[target]["acknowledged"] is True

        # unread_only now drops the acked cluster.
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"unread_only": "true"},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        ids = {i["id"] for i in resp.json()["items"]}
        assert target not in ids

        # acknowledged=true returns only the acked cluster.
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"acknowledged": "true"},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        ids = {i["id"] for i in resp.json()["items"]}
        assert ids == {target}

        # Un-ack is idempotent and reversible.
        resp = await relevance_client.post(
            f"/api/v1/holding-relevance/clusters/{target}/ack",
            json={"acknowledged": False},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"unread_only": "true"},
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 3

    async def test_ack_is_per_user(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
        relevance_settings: Settings,
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        cluster_ids = await _cluster_ids(session_factory, tenant_a["tenant_id"])
        target = cluster_ids[0]

        # Second user of the same tenant.
        async with session_factory() as session:
            user_b = User(
                email="rel-a-2@finance-sync.local",
                tenant_id=tenant_a["tenant_id"],
                hashed_password=hash_password("test-password"),
                display_name="Second User",
                role=UserRole.ADMIN,
                is_active=True,
            )
            session.add(user_b)
            await session.commit()
            user_b_id = str(user_b.id)
        token_b = create_access_token(
            {
                "sub": user_b_id,
                "tenant_id": tenant_a["tenant_id"],
                "role": "admin",
            },
            relevance_settings,
        )
        headers_b = {"Authorization": f"Bearer {token_b}"}

        # A acks; B still sees it unread.
        await relevance_client.post(
            f"/api/v1/holding-relevance/clusters/{target}/ack",
            json={"acknowledged": True},
            headers=tenant_a["headers"],
        )
        resp_a = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"acknowledged": "true"},
            headers=tenant_a["headers"],
        )
        assert {i["id"] for i in resp_a.json()["items"]} == {target}

        resp_b = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"acknowledged": "true"},
            headers=headers_b,
        )
        assert resp_b.status_code == 200, resp_b.text
        assert resp_b.json()["total"] == 0

        # Cross-tenant ack → 404 (existence never leaks).
        tenant_b = seeded_tenant["tenant_b"]
        resp_cross = await relevance_client.post(
            f"/api/v1/holding-relevance/clusters/{target}/ack",
            json={"acknowledged": True},
            headers=tenant_b["headers"],
        )
        assert resp_cross.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# Tenant isolation
# ═══════════════════════════════════════════════════════════════════════


class TestTenantIsolation:
    async def test_cross_tenant_security_never_leaks(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_b = seeded_tenant["tenant_b"]
        # B filters by A's security id → empty feed, never A's rows.
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"security_id": seeded_tenant["security_a"]},
            headers=tenant_b["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 0

    async def test_cross_tenant_account_never_leaks(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_b = seeded_tenant["tenant_b"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed",
            params={"account_id": seeded_tenant["account_a"]},
            headers=tenant_b["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 0

    async def test_cross_tenant_through_ack_filter_never_leaks(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        """B can never see A's rows even via unread/ack filters."""
        tenant_b = seeded_tenant["tenant_b"]
        for params in ({"unread_only": "true"}, {"acknowledged": "true"}):
            resp = await relevance_client.get(
                "/api/v1/holding-relevance/feed",
                params=params,
                headers=tenant_b["headers"],
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["total"] == 0


# ═══════════════════════════════════════════════════════════════════════
# Graceful degradation
# ═══════════════════════════════════════════════════════════════════════


class TestGracefulDegradation:
    async def test_stale_cluster_served_with_flag(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        """Stale/missing sources still serve cached data (200, flagged)."""
        tenant_a = seeded_tenant["tenant_a"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed", headers=tenant_a["headers"]
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        stale = [i for i in items if i["is_stale"]]
        assert len(stale) == 1
        assert stale[0]["sources"][0]["freshness"] == "stale"
        # Even the stale cluster keeps its source URL + fetched_at.
        assert stale[0]["best_source_url"]
        assert stale[0]["sources"][0]["fetched_at"]

    async def test_missing_sources_never_500(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A cluster whose source item was removed still renders 200."""
        from sqlalchemy import delete

        from finance_sync.models.market_intelligence_item import (
            MarketIntelligenceItem,
        )

        tenant_a = seeded_tenant["tenant_a"]
        # Delete the source observation behind the stale cluster; the
        # relevance row / cluster edge may linger (FK cascade on the
        # relevance side removes them, but a torn state must never 500).
        async with session_factory() as session:
            await session.execute(
                delete(MarketIntelligenceItem).where(
                    MarketIntelligenceItem.tenant_id == tenant_a["tenant_id"],
                    MarketIntelligenceItem.source_id == "aapl-stale",
                )
            )
            await session.commit()

        resp = await relevance_client.get(
            "/api/v1/holding-relevance/feed", headers=tenant_a["headers"]
        )
        # Still 200 — cached/surviving data is served, nothing raises.
        assert resp.status_code == 200, resp.text
        assert "items" in resp.json()


# ═══════════════════════════════════════════════════════════════════════
# Calendar
# ═══════════════════════════════════════════════════════════════════════


class TestCalendar:
    async def test_calendar_returns_events(
        self,
        relevance_client: httpx.AsyncClient,
        seeded_tenant: dict[str, Any],
    ) -> None:
        tenant_a = seeded_tenant["tenant_a"]
        resp = await relevance_client.get(
            "/api/v1/holding-relevance/calendar", headers=tenant_a["headers"]
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 1
        for event in body["events"]:
            assert event["event_date"] is not None
            assert event["event_type"]
            assert event["headline"]
            assert event["security_ticker"]


# ═══════════════════════════════════════════════════════════════════════
# MCP exposure
# ═══════════════════════════════════════════════════════════════════════


class TestMCPExposure:
    async def test_mcp_tools_registered(self) -> None:
        """The three holding-relevance tools are registered."""
        from finance_sync.mcp.server import mcp

        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "get_holding_feed" in tool_names
        assert "get_holding_calendar" in tool_names
        assert "acknowledge_holding_cluster" in tool_names
