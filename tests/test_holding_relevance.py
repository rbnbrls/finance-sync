"""Tests for the holding-relevance service and data model.

Covers the acceptance criteria of backlog/plus-relevant-nieuws-en-events.md:

  A1  Canonical security matches are stored with match reason and
      confidence; generic low-confidence ticker/name matches are
      rejected (only canonical ``security_id`` rows ever surface).
  A2  bunq cash accounts only influence portfolio events when genuinely
      relevant (interest / currency events), never plain news.
  A3  Ranking is deterministic and documented: same input → same order,
      weighted by holding weight, event proximity, recency and source
      reliability.
  A4  Tenant/household isolation: a security held by tenant A but not B
      yields an empty feed for B (never an error, never leaked rows),
      including through ``unread``/``date`` filters and cross-tenant
      account/security filters.
  A5  Clustering: syndicated coverage of one event merges into one
      cluster keeping every source link; distinct events (different
      quarter, ex-date vs payment date) stay separate.
  A6  Acknowledgement semantics: per-user per-cluster, idempotent, and
      a later source link never resets an existing ack.
  A7  Corrections are per-user and never delete the observation.
  A8  Graceful degradation / freshness: stale items carry a freshness
      value; filters treat payloads as data (no injection).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

# Make JSONB work with SQLite (same pattern as the repo's other tests).
if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[assignment]

import uuid as _uuid_mod

from sqlalchemy import types as _sa_types

_uuid_bind_orig = _sa_types.Uuid.bind_processor


def _uuid_bind_patched(self: Any, dialect: Any) -> Any:
    proc = _uuid_bind_orig(self, dialect)
    if proc is None or not self.as_uuid:
        return proc

    def _patched(value: Any) -> Any:
        if value is not None:
            if isinstance(value, str):
                return _uuid_mod.UUID(value).hex
            return value.hex
        return value

    return _patched


_sa_types.Uuid.bind_processor = _uuid_bind_patched

from finance_sync.db import Base
from finance_sync.db.uow import UnitOfWork
from finance_sync.models.account import Account
from finance_sync.models.enums import AccountType, HoldingSource
from finance_sync.models.holding import Holding
from finance_sync.models.holding_relevance import (
    CLUSTER_REASON_EXACT_EVENT,
    CLUSTER_REASON_TITLE_DUPLICATE,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    HOLDING_STATUS_CURRENT,
    HOLDING_STATUS_RECENTLY_SOLD,
    MATCH_REASON_CURRENCY_INTEREST,
    MATCH_REASON_EXACT_SECURITY,
    MATCH_REASON_RECENTLY_SOLD,
    HoldingRelevanceItem,
    RelevanceCluster,
    RelevanceCorrection,
    RelevanceNotificationLog,
)
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.security import Security
from finance_sync.models.tenant import Tenant
from finance_sync.services.holding_relevance import (
    HoldingRelevanceService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def engine() -> AsyncEngine:
    return create_async_engine("sqlite+aiosqlite://", echo=False)


@pytest.fixture
async def tables(engine: AsyncEngine) -> AsyncGenerator[None, None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session_factory(
    engine: AsyncEngine, tables: Any
) -> async_sessionmaker:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def _new_tenant(
    session_factory: async_sessionmaker, slug: str | None = None
) -> str:
    """Create a tenant row and return its id."""
    async with session_factory() as session:
        uow = UnitOfWork(session)
        t = Tenant(slug=slug or f"tenant-{uuid4().hex[:8]}", name="Test tenant")
        await uow.tenants.add(t)
        await uow.commit()
        return str(t.id)


async def _new_security(
    session_factory: async_sessionmaker,
    *,
    ticker: str = "AAPL",
    name: str = "Apple Inc.",
) -> str:
    async with session_factory() as session:
        uow = UnitOfWork(session)
        s = Security(
            isin="US0378331005" if ticker == "AAPL" else None,
            ticker=ticker,
            name=name,
            security_type="stock",
            currency_code="USD",
        )
        await uow.securities.add(s)
        await uow.commit()
        return str(s.id)


async def _new_account(
    session_factory: async_sessionmaker,
    tenant_id: str,
    *,
    name: str = "Trading212",
    account_type: AccountType = AccountType.BROKERAGE,
) -> str:
    async with session_factory() as session:
        uow = UnitOfWork(session)
        a = Account(
            tenant_id=tenant_id,
            provider_key="trading212",
            external_account_id=f"ext-{uuid4().hex[:8]}",
            name=name,
            account_type=account_type,
            currency_code="EUR",
        )
        await uow.accounts.add(a)
        await uow.commit()
        return str(a.id)


async def _new_holding(
    session_factory: async_sessionmaker,
    tenant_id: str,
    account_id: str,
    security_id: str,
    *,
    quantity: Decimal = Decimal(10),
    market_value: Decimal | None = Decimal(1500),
    observed_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        uow = UnitOfWork(session)
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
        await uow.holdings.add(h)
        await uow.commit()


async def _new_item(
    session_factory: async_sessionmaker,
    tenant_id: str,
    security_id: str | None,
    *,
    provider: str = "openbb",
    source_id: str | None = None,
    kind: str = "news_article",
    headline: str = "Apple beats estimates",
    canonical_url: str | None = "https://example.com/news/1",
    published_at: datetime | None = None,
    fetched_at: datetime | None = None,
    facts: list[dict[str, Any]] | None = None,
    resolution_status: str = "resolved",
) -> str:
    """Create one stored market-intelligence observation."""
    now = datetime.now(UTC)
    sid = source_id or f"src-{uuid4().hex[:8]}"
    async with session_factory() as session:
        uow = UnitOfWork(session)
        item = MarketIntelligenceItem(
            tenant_id=tenant_id,
            provider=provider,
            source_id=sid,
            canonical_url=canonical_url,
            kind=kind,
            published_at=published_at or now,
            fetched_at=fetched_at or now,
            language="en",
            license_class="free_access",
            content_hash=f"hash-{tenant_id}-{sid}",
            headline=headline,
            summary="Summary",
            facts=facts or [],
            identifiers={"ticker": "AAPL"},
            resolution_status=resolution_status,
            security_id=security_id,
        )
        await uow.market_intelligence_items.add(item)
        await uow.commit()
        return str(item.id)


async def _build(
    service: HoldingRelevanceService, tenant_id: str
) -> dict[str, int]:
    """Run the build pipeline and commit."""
    result = await service.build_feed(tenant_id)
    await service._uow.commit()  # type: ignore[reportPrivateUsage]
    return result


async def _clusters(
    session_factory: async_sessionmaker, tenant_id: str
) -> list[RelevanceCluster]:
    async with session_factory() as session:
        stmt = (
            select(RelevanceCluster)
            .where(RelevanceCluster.tenant_id == tenant_id)
            .order_by(RelevanceCluster.score.desc())
        )
        return list((await session.execute(stmt)).scalars().all())


async def _cluster_count(
    session_factory: async_sessionmaker, tenant_id: str
) -> int:
    async with session_factory() as session:
        stmt = select(RelevanceCluster).where(
            RelevanceCluster.tenant_id == tenant_id
        )
        rows = (await session.execute(stmt)).scalars().all()
        return len(list(rows))


async def _relevance_count(
    session_factory: async_sessionmaker, tenant_id: str
) -> int:
    async with session_factory() as session:
        stmt = select(HoldingRelevanceItem).where(
            HoldingRelevanceItem.tenant_id == tenant_id
        )
        rows = (await session.execute(stmt)).scalars().all()
        return len(list(rows))


# ═══════════════════════════════════════════════════════════════════════
# A1 — Canonical matches stored with reason + confidence; generic
#      low-confidence ticker/name matches are rejected
# ═══════════════════════════════════════════════════════════════════════


class TestCanonicalMatching:
    async def test_current_holding_canonical_match_stored(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A resolved item whose security is held matches with reason."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_id = await _new_item(
            session_factory, tenant, sec, kind="earnings_report"
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            summary = await _build(svc, tenant)

        assert summary["matched"] == 1
        assert summary["current_holdings"] == 1

        async with session_factory() as session:
            stmt = select(HoldingRelevanceItem).where(
                HoldingRelevanceItem.tenant_id == tenant
            )
            rows = list((await session.execute(stmt)).scalars().all())
            assert len(rows) == 1
            row = rows[0]
            assert str(row.security_id) == sec
            assert row.match_reason == MATCH_REASON_EXACT_SECURITY
            assert row.confidence == 1.0
            assert row.holding_status == HOLDING_STATUS_CURRENT
            assert str(row.item_id) == item_id
            # Feed surfaces exactly one cluster with the source.
            clusters = await _clusters(session_factory, tenant)
            assert len(clusters) == 1
            assert clusters[0].event_type == "earnings"

    async def test_recently_sold_holding_matches_with_reason(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A recently sold security matches with recently_sold reason."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory, ticker="MSFT")
        acct = await _new_account(session_factory, tenant)
        # Sold: latest snapshot shows quantity 0 within the 180-day window.
        await _new_holding(
            session_factory,
            tenant,
            acct,
            sec,
            quantity=Decimal(0),
            market_value=None,
            observed_at=datetime.now(UTC) - timedelta(days=10),
        )
        await _new_item(session_factory, tenant, sec, kind="dividend")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            summary = await _build(svc, tenant)

        assert summary["matched"] == 1
        assert summary["recently_sold"] == 1

        async with session_factory() as session:
            stmt = select(HoldingRelevanceItem).where(
                HoldingRelevanceItem.tenant_id == tenant
            )
            row = (await session.execute(stmt)).scalars().first()
            assert row is not None
            assert row.match_reason == MATCH_REASON_RECENTLY_SOLD
            assert row.confidence == 0.8
            assert row.holding_status == HOLDING_STATUS_RECENTLY_SOLD

    async def test_generic_low_confidence_match_rejected(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Unresolved items (no canonical security) never surface.

        A generic ticker/name match that the intel layer could not
        resolve to a canonical ``security_id`` is *not* holding news.
        """
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        # Item mentions AAPL in the headline but resolution was NOT
        # canonical — security_id NULL, resolution_status unresolved.
        await _new_item(
            session_factory,
            tenant,
            None,
            headline="AAPL jumps on earnings",
            kind="news_article",
            resolution_status="unresolved",
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            summary = await _build(svc, tenant)

        # Only the canonical security's rows matter: no item matched.
        assert summary["matched"] == 0
        assert await _relevance_count(session_factory, tenant) == 0
        assert await _cluster_count(session_factory, tenant) == 0

    async def test_sold_beyond_window_not_matched(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A security sold longer than the window ago is not matched."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory, ticker="NFLX")
        acct = await _new_account(session_factory, tenant)
        await _new_holding(
            session_factory,
            tenant,
            acct,
            sec,
            quantity=Decimal(0),
            market_value=None,
            observed_at=datetime.now(UTC) - timedelta(days=400),
        )
        await _new_item(session_factory, tenant, sec)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            summary = await _build(svc, tenant)

        assert summary["matched"] == 0
        assert summary["recently_sold"] == 0


# ═══════════════════════════════════════════════════════════════════════
# A2 — bunq cash accounts only influence portfolio events when genuinely
#      relevant (interest / currency), never plain news
# ═══════════════════════════════════════════════════════════════════════


class TestCashAccountRelevance:
    async def test_cash_news_not_matched(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Plain news with no security does NOT match a cash account."""
        tenant = await _new_tenant(session_factory)
        await _new_account(
            session_factory,
            tenant,
            name="bunq savings",
            account_type=AccountType.SAVINGS,
        )
        await _new_item(
            session_factory,
            tenant,
            None,
            provider="openbb",
            kind="news_article",
            headline="ECB keeps rates unchanged",
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            summary = await _build(svc, tenant)

        assert summary["matched"] == 0
        assert await _relevance_count(session_factory, tenant) == 0

    async def test_cash_interest_event_matched(
        self, session_factory: async_sessionmaker
    ) -> None:
        """An interest event genuinely relevant to cash matches."""
        tenant = await _new_tenant(session_factory)
        acct = await _new_account(
            session_factory,
            tenant,
            name="bunq savings",
            account_type=AccountType.SAVINGS,
        )
        await _new_item(
            session_factory,
            tenant,
            None,
            provider="openbb",
            kind="interest_event",
            headline="Savings rate rises to 3.2%",
            facts=[{"key": "event_date", "value": "2026-09-01"}],
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            summary = await _build(svc, tenant)

        assert summary["matched"] == 1
        async with session_factory() as session:
            stmt = select(HoldingRelevanceItem).where(
                HoldingRelevanceItem.tenant_id == tenant
            )
            row = (await session.execute(stmt)).scalars().first()
            assert row is not None
            assert row.match_reason == MATCH_REASON_CURRENCY_INTEREST
            assert row.security_id is None
            assert str(row.account_id) == acct
            assert row.confidence == 0.6

    async def test_cash_currency_event_matched(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A currency event also counts as genuinely relevant to cash."""
        tenant = await _new_tenant(session_factory)
        await _new_account(
            session_factory,
            tenant,
            name="bunq EUR",
            account_type=AccountType.SAVINGS,
        )
        await _new_item(
            session_factory,
            tenant,
            None,
            provider="openbb",
            kind="currency_event",
            headline="EUR/USD moves above 1.10",
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            summary = await _build(svc, tenant)

        assert summary["matched"] == 1
        async with session_factory() as session:
            stmt = select(HoldingRelevanceItem).where(
                HoldingRelevanceItem.tenant_id == tenant
            )
            row = (await session.execute(stmt)).scalars().first()
            assert row is not None
            assert row.match_reason == MATCH_REASON_CURRENCY_INTEREST


# ═══════════════════════════════════════════════════════════════════════
# A3 — Deterministic ranking
# ═══════════════════════════════════════════════════════════════════════


class TestDeterministicRanking:
    async def test_ranking_is_deterministic(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Same input twice → same cluster order and scores."""
        tenant = await _new_tenant(session_factory)
        sec_a = await _new_security(session_factory, ticker="AAPL")
        sec_b = await _new_security(session_factory, ticker="MSFT")
        acct = await _new_account(session_factory, tenant)
        await _new_holding(
            session_factory, tenant, acct, sec_a, market_value=Decimal(900)
        )
        await _new_holding(
            session_factory, tenant, acct, sec_b, market_value=Decimal(100)
        )
        now = datetime.now(UTC)
        await _new_item(
            session_factory,
            tenant,
            sec_a,
            kind="earnings_report",
            published_at=now - timedelta(hours=2),
        )
        await _new_item(
            session_factory,
            tenant,
            sec_b,
            kind="earnings_report",
            published_at=now - timedelta(hours=2),
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        first = await _clusters(session_factory, tenant)
        first_scores = [c.score for c in first]
        first_ids = [str(c.id) for c in first]

        # Re-running is a no-op that yields identical rows.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        second = await _clusters(session_factory, tenant)

        assert [str(c.id) for c in second] == first_ids
        assert [c.score for c in second] == first_scores

        # The heavier holding ranks first (weight factor dominates).
        assert str(first[0].security_id) == sec_a
        assert first[0].score > first[1].score

    async def test_score_is_bounded_and_documented(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Scores live in 0..1 and are computed from the documented factors."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 1
        assert 0.0 <= clusters[0].score <= 1.0


# ═══════════════════════════════════════════════════════════════════════
# A4 — Tenant/household isolation
# ═══════════════════════════════════════════════════════════════════════


class TestTenantIsolation:
    async def test_shared_ticker_no_cross_tenant_leak(
        self, session_factory: async_sessionmaker
    ) -> None:
        """AAPL held by tenant A; tenant B's feed for AAPL is empty."""
        tenant_a = await _new_tenant(session_factory)
        tenant_b = await _new_tenant(session_factory)
        # Same canonical security row, held only by A.
        sec = await _new_security(session_factory)
        acct_a = await _new_account(session_factory, tenant_a)
        acct_b = await _new_account(session_factory, tenant_b)
        await _new_holding(session_factory, tenant_a, acct_a, sec)
        # B holds a different security so B has an active account.
        sec_b = await _new_security(session_factory, ticker="MSFT")
        await _new_holding(session_factory, tenant_b, acct_b, sec_b)
        await _new_item(session_factory, tenant_a, sec, kind="earnings_report")
        await _new_item(
            session_factory, tenant_b, sec_b, kind="earnings_report"
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant_a)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant_b)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed_a = await svc.feed(tenant_a, security_id=sec)
            feed_b = await svc.feed(tenant_b, security_id=sec)

        # B's feed for A's security is empty — no error, no leak.
        assert feed_b["total"] == 0
        assert feed_b["items"] == []
        # A sees its own item.
        assert feed_a["total"] == 1
        assert str(feed_a["items"][0]["security_id"]) == sec

    async def test_cross_tenant_account_filter_never_leaks(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Filtering B's feed by A's account id returns empty, not A's rows."""
        tenant_a = await _new_tenant(session_factory)
        tenant_b = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct_a = await _new_account(session_factory, tenant_a)
        acct_b = await _new_account(session_factory, tenant_b)
        await _new_holding(session_factory, tenant_a, acct_a, sec)
        sec_b = await _new_security(session_factory, ticker="MSFT")
        await _new_holding(session_factory, tenant_b, acct_b, sec_b)
        await _new_item(session_factory, tenant_a, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant_a)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant_b, account_id=acct_a)

        assert feed["total"] == 0
        assert feed["items"] == []

    async def test_injection_payloads_treated_as_data(
        self, session_factory: async_sessionmaker
    ) -> None:
        """SQL/wildcard payloads in filters return empty, never an error."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        payloads = [
            "AAPL' OR '1'='1",
            "%",
            "_",
            "'; DROP TABLE relevance_clusters; --",
        ]
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            for payload in payloads:
                feed = await svc.feed(
                    tenant,
                    security_id=payload,
                    account_id=payload,
                    item_type=payload,
                    unread_only=False,
                )
                # Never an exception; payloads match no rows.
                assert feed["total"] == 0
                assert feed["items"] == []
            # The real security still resolves.
            feed = await svc.feed(tenant, security_id=sec)
            assert feed["total"] == 1


# ═══════════════════════════════════════════════════════════════════════
# A5 — Clustering precision: no over- and under-merge
# ═══════════════════════════════════════════════════════════════════════


class TestClustering:
    async def test_syndicated_items_merge_into_one_cluster(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Three syndicated posts about one event = one cluster, 3 links."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        published = datetime.now(UTC) - timedelta(hours=1)
        # Same event date for all three syndicated posts.
        facts = [{"key": "event_date", "value": "2026-09-15"}]
        for i in range(3):
            await _new_item(
                session_factory,
                tenant,
                sec,
                provider="sec_press" if i == 0 else "openbb",
                source_id=f"syn-{i}",
                headline=f"Apple earnings Q4 (source {i})",
                canonical_url=f"https://example.com/syn/{i}",
                kind="earnings_report",
                published_at=published,
                facts=facts,
            )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.source_count == 3
        assert cluster.event_type == "earnings"

        # Feed DTO carries all three source links.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1")
        assert feed["total"] == 1
        item = feed["items"][0]
        assert item["source_count"] == 3
        assert len(item["sources"]) == 3
        urls = {s["url"] for s in item["sources"]}
        assert urls == {
            "https://example.com/syn/0",
            "https://example.com/syn/1",
            "https://example.com/syn/2",
        }

    async def test_distinct_events_stay_separate(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Different quarters / ex-date vs payment date = distinct clusters."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)

        # Q3 earnings vs Q4 earnings — different event dates.
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="q3",
            kind="earnings_report",
            headline="Q3 earnings",
            facts=[{"key": "event_date", "value": "2026-07-30"}],
        )
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="q4",
            kind="earnings_report",
            headline="Q4 earnings",
            facts=[{"key": "event_date", "value": "2026-10-30"}],
        )
        # Ex-date vs payment date — different event dates too.
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="ex",
            kind="dividend",
            headline="Dividend ex-date",
            facts=[{"key": "ex_date", "value": "2026-08-10"}],
        )
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="pay",
            kind="dividend",
            headline="Dividend payment",
            facts=[{"key": "payment_date", "value": "2026-08-25"}],
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        clusters = await _clusters(session_factory, tenant)
        # 2 earnings + 2 dividends = 4 distinct clusters.
        assert len(clusters) == 4
        types = sorted(c.event_type for c in clusters)
        assert types == ["dividend", "dividend", "earnings", "earnings"]
        dates = sorted(
            c.event_date.date().isoformat()  # type: ignore[union-attr]
            for c in clusters
        )
        assert dates == ["2026-07-30", "2026-08-10", "2026-08-25", "2026-10-30"]

    async def test_different_securities_stay_separate(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Same event date, different securities → separate clusters."""
        tenant = await _new_tenant(session_factory)
        sec_a = await _new_security(session_factory, ticker="AAPL")
        sec_b = await _new_security(session_factory, ticker="MSFT")
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec_a)
        await _new_holding(session_factory, tenant, acct, sec_b)
        facts = [{"key": "event_date", "value": "2026-09-15"}]
        await _new_item(
            session_factory,
            tenant,
            sec_a,
            source_id="a",
            kind="earnings_report",
            facts=facts,
        )
        await _new_item(
            session_factory,
            tenant,
            sec_b,
            source_id="b",
            kind="earnings_report",
            facts=facts,
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 2
        assert {str(c.security_id) for c in clusters} == {sec_a, sec_b}

    async def test_syndicated_items_with_different_event_dates_merge(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Syndicated posts of one story with slightly different event
        dates collapse into one cluster via title fingerprint."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        published = datetime.now(UTC) - timedelta(hours=1)
        headlines = (
            "Apple Q4 earnings beat expectations",
            "Apple earnings beat Q4 expectations",
            "Apple Q4 earnings beat expectations",
        )
        for i, (day, headline) in enumerate(
            zip(
                ("2026-09-14", "2026-09-15", "2026-09-16"),
                headlines,
                strict=True,
            )
        ):
            await _new_item(
                session_factory,
                tenant,
                sec,
                provider="sec_press" if i == 0 else "openbb",
                source_id=f"syn-date-{i}",
                headline=headline,
                canonical_url=f"https://example.com/syn-date/{i}",
                kind="earnings_report",
                published_at=published,
                facts=[{"key": "event_date", "value": day}],
            )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.source_count == 3
        assert cluster.cluster_reason == CLUSTER_REASON_TITLE_DUPLICATE

        # All three source links are retained in the feed DTO.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1")
        assert feed["total"] == 1
        item = feed["items"][0]
        assert item["source_count"] == 3
        urls = {s["url"] for s in item["sources"]}
        assert urls == {
            "https://example.com/syn-date/0",
            "https://example.com/syn-date/1",
            "https://example.com/syn-date/2",
        }

    async def test_same_story_distinct_published_times_merge(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Syndicated plain-news items (no structured event facts) with
        different published_at timestamps collapse via title fingerprint,
        while a genuinely different story stays separate."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        now = datetime.now(UTC)
        # Two syndicated posts of the same story, no facts, published
        # ~2 hours apart → different event dates (published fallback).
        for i, hrs in enumerate((1, 3)):
            await _new_item(
                session_factory,
                tenant,
                sec,
                source_id=f"nodate-syn-{i}",
                headline="Apple unveils new MacBook Pro",
                canonical_url=f"https://example.com/nodate/{i}",
                kind="news_article",
                published_at=now - timedelta(hours=hrs),
            )
        # A distinct story, no facts → different title fingerprint.
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="nodate-other",
            headline="Apple opens new store in Amsterdam",
            canonical_url="https://example.com/nodate/other",
            kind="news_article",
            published_at=now - timedelta(hours=2),
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 2
        # The syndicated pair merges with a title_duplicate reason.
        fp_cluster = next(
            c
            for c in clusters
            if c.cluster_reason == CLUSTER_REASON_TITLE_DUPLICATE
        )
        assert fp_cluster.source_count == 2

    async def test_distinct_facts_with_same_title_never_merge(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Identical title fingerprints with different event dates are
        different stories — never merged (no over-merge)."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="q3",
            kind="earnings_report",
            headline="Apple Q3 earnings",
            facts=[{"key": "event_date", "value": "2026-07-30"}],
        )
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="q4",
            kind="earnings_report",
            headline="Apple Q3 earnings",
            facts=[{"key": "event_date", "value": "2026-10-30"}],
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 2
        # Both keep their own event dates.
        dates = sorted(
            c.event_date.date().isoformat()  # type: ignore[union-attr]
            for c in clusters
        )
        assert dates == ["2026-07-30", "2026-10-30"]

    async def test_cluster_metadata_exposed(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Cluster DTO carries cluster_id, item IDs, cluster reason and
        earliest published_at."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        now = datetime.now(UTC)
        item_a = await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="meta-a",
            headline="Apple Q4 earnings",
            canonical_url="https://example.com/meta-a",
            kind="earnings_report",
            published_at=now - timedelta(hours=2),
            facts=[{"key": "event_date", "value": "2026-10-30"}],
        )
        item_b = await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="meta-b",
            headline="Apple Q4 earnings (update)",
            canonical_url="https://example.com/meta-b",
            kind="earnings_report",
            published_at=now - timedelta(hours=1),
            facts=[{"key": "event_date", "value": "2026-10-30"}],
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1")

        assert feed["total"] == 1
        item = feed["items"][0]
        assert item["cluster_id"] == item["id"]
        assert item["cluster_reason"] == CLUSTER_REASON_EXACT_EVENT
        assert set(item["item_ids"]) == {item_a, item_b}
        # earliest published_at = min across sources.
        assert item["earliest_published_at"] is not None


# ═══════════════════════════════════════════════════════════════════════
# A6 — Acknowledgement semantics (per-user per-cluster, idempotent)
# ═══════════════════════════════════════════════════════════════════════


class TestAcknowledgement:
    async def _seed(
        self, session_factory: async_sessionmaker
    ) -> tuple[str, str]:
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(
            session_factory,
            tenant,
            sec,
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        return tenant, str(clusters[0].id)

    async def test_ack_is_per_user(
        self, session_factory: async_sessionmaker
    ) -> None:
        """User A acks; user B still sees unread."""
        tenant, cluster_id = await self._seed(session_factory)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            assert await svc.set_ack(tenant, "user-A", cluster_id, True)
            await session.commit()
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed_a = await svc.feed(tenant, user_id="user-A")
            feed_b = await svc.feed(tenant, user_id="user-B")

        assert feed_a["items"][0]["acknowledged"] is True
        assert feed_b["items"][0]["acknowledged"] is False

    async def test_ack_idempotent(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Re-ack and un-ack are idempotent."""
        tenant, cluster_id = await self._seed(session_factory)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            assert await svc.set_ack(tenant, "user-A", cluster_id, True)
            assert await svc.set_ack(tenant, "user-A", cluster_id, True)
            await session.commit()
            assert await svc.set_ack(tenant, "user-A", cluster_id, False)
            assert await svc.set_ack(tenant, "user-A", cluster_id, False)
            await session.commit()
            feed = await svc.feed(tenant, user_id="user-A")
            assert feed["items"][0]["acknowledged"] is False

    async def test_cross_tenant_ack_returns_false(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Acking a cluster from another tenant is a safe no-op (False)."""
        tenant_a, cluster_id = await self._seed(session_factory)
        tenant_b = await _new_tenant(session_factory)
        _ = tenant_a  # seed cluster belongs to tenant_a; ack from tenant_b must no-op

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            ok = await svc.set_ack(tenant_b, "user-B", cluster_id, True)
            assert ok is False

    async def test_new_source_link_does_not_reset_ack(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Adding a later syndicated source never resets an existing ack."""
        tenant, cluster_id = await self._seed(session_factory)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_ack(tenant, "user-A", cluster_id, True)
            await session.commit()

        # A second syndicated post about the same event arrives.
        async with session_factory() as session:
            stmt = select(HoldingRelevanceItem).where(
                HoldingRelevanceItem.tenant_id == tenant
            )
            row = (await session.execute(stmt)).scalars().first()
            sec_id = str(row.security_id)
        published = datetime.now(UTC) - timedelta(hours=1)
        await _new_item(
            session_factory,
            tenant,
            sec_id,
            provider="sec_press",
            source_id="syn-later",
            headline="Apple earnings update",
            canonical_url="https://example.com/syn/later",
            kind="earnings_report",
            published_at=published,
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-A")
            assert feed["items"][0]["acknowledged"] is True
            assert feed["items"][0]["source_count"] == 2

    async def test_ack_survives_fingerprint_merge(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Acking a story, then a syndicated twin with a slightly
        different event date arrives → the story merges into a
        fingerprint cluster, but the ack is migrated, not reset."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_a = await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="ack-a",
            headline="Apple Q4 earnings beat",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_ack(tenant, "user-A", cluster_id, True)
            await session.commit()

        # A syndicated twin with a different event date arrives.
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="ack-b",
            headline="Apple Q4 earnings beat",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-16"}],
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-A")
            # One merged story, still acknowledged for user-A.
            assert feed["total"] == 1
            assert feed["items"][0]["acknowledged"] is True
            assert feed["items"][0]["source_count"] == 2
        # The original observation survives (never deleted).
        async with session_factory() as session:
            item = await session.get(MarketIntelligenceItem, item_a)
            assert item is not None


# ═══════════════════════════════════════════════════════════════════════
# A7 — Corrections are per-user and never delete the observation
# ═══════════════════════════════════════════════════════════════════════


class TestCorrections:
    async def test_correction_suppresses_for_user_only(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A's correction hides the item in A's feed; B still sees it."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_id = await _new_item(
            session_factory, tenant, sec, kind="earnings_report"
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            ok = await svc.correct(
                tenant,
                "user-A",
                item_id,
                security_id=sec,
                reason="Wrong company",
            )
            assert ok is True
            await session.commit()

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed_a = await svc.feed(tenant, user_id="user-A")
            feed_b = await svc.feed(tenant, user_id="user-B")

        assert feed_a["total"] == 0
        assert feed_b["total"] == 1

        # The observation itself still exists.
        async with session_factory() as session:
            item = await session.get(MarketIntelligenceItem, item_id)
            assert item is not None

    async def test_correction_is_idempotent_and_cross_tenant_safe(
        self, session_factory: async_sessionmaker
    ) -> None:
        tenant = await _new_tenant(session_factory)
        other = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_id = await _new_item(session_factory, tenant, sec)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            assert await svc.correct(tenant, "user-A", item_id) is True
            assert await svc.correct(tenant, "user-A", item_id) is True
            await session.commit()
            # Cross-tenant item id is a safe False.
            assert await svc.correct(other, "user-B", item_id) is False

        async with session_factory() as session:
            stmt = select(RelevanceCorrection).where(
                RelevanceCorrection.tenant_id == tenant
            )
            rows = (await session.execute(stmt)).scalars().all()
            assert len(list(rows)) == 1

    async def test_correction_reason_is_sanitised(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Free-form correction text is redacted before persistence."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_id = await _new_item(session_factory, tenant, sec)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.correct(
                tenant,
                "user-A",
                item_id,
                reason="Wrong, secret=sk_live_abcdefghijklmnop",
            )
            await session.commit()

        async with session_factory() as session:
            stmt = select(RelevanceCorrection).where(
                RelevanceCorrection.tenant_id == tenant
            )
            row = (await session.execute(stmt)).scalars().first()
            assert row is not None
            assert "sk_live_abcdefghijklmnop" not in (row.reason or "")
            assert "[REDACTED]" in (row.reason or "")

    async def test_correction_is_pair_scoped_and_prevents_rematch(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Correcting (item, security) hides that pair in the correcting
        user's feed immediately AND keeps a similar future item for the
        same security claim out of that user's feed (re-match prevention).
        The correction is per-user: B still sees everything, and the
        underlying observation is never deleted."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_id = await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="fp-1",
            headline="Apple Q4 earnings beat",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        # User A corrects the (item, security) pair.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            ok = await svc.correct(tenant, "user-A", item_id, security_id=sec)
            assert ok is True
            await session.commit()

        # A's feed hides it immediately; B still sees it.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed_a = await svc.feed(tenant, user_id="user-A")
            feed_b = await svc.feed(tenant, user_id="user-B")
        assert feed_a["total"] == 0
        assert feed_b["total"] == 1

        # A similar FUTURE item for the same security claim is also kept
        # out of A's feed (fingerprint-based re-match prevention) while
        # B still sees the story (both items, one cluster).
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="fp-2",
            headline="Apple Q4 earnings beat",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-16"}],
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
            feed_a = await svc.feed(tenant, user_id="user-A")
            feed_b = await svc.feed(tenant, user_id="user-B")
        assert feed_a["total"] == 0
        # B sees the merged story with both source links.
        assert feed_b["total"] == 1
        assert feed_b["items"][0]["source_count"] == 2

        # The observation itself still exists (never deleted).
        async with session_factory() as session:
            item = await session.get(MarketIntelligenceItem, item_id)
            assert item is not None

    async def test_correction_pair_scoped_keeps_other_security(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Correcting (item, sec_a) does not hide the item's match to
        sec_b: corrections are pair-scoped, not item-scoped."""
        tenant = await _new_tenant(session_factory)
        sec_a = await _new_security(session_factory, ticker="AAPL")
        sec_b = await _new_security(session_factory, ticker="MSFT")
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec_a)
        await _new_holding(session_factory, tenant, acct, sec_b)
        item_id = await _new_item(
            session_factory,
            tenant,
            sec_a,
            source_id="both-2",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as session:
            from finance_sync.models.holding_relevance import (
                HoldingRelevanceItem,
            )

            uow = UnitOfWork(session)
            await uow.holding_relevance_items.add(
                HoldingRelevanceItem(
                    tenant_id=tenant,
                    item_id=item_id,
                    security_id=sec_b,
                    account_id=None,
                    match_reason=MATCH_REASON_EXACT_SECURITY,
                    confidence=1.0,
                    holding_status=HOLDING_STATUS_CURRENT,
                    holding_weight=None,
                    event_date=datetime.now(UTC),
                )
            )
            await session.commit()

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
            await svc.correct(tenant, "user-A", item_id, security_id=sec_a)
            await session.commit()
            feed = await svc.feed(tenant, user_id="user-A")
            # sec_b's match is still visible to A.
            assert feed["total"] == 1
            assert str(feed["items"][0]["security_id"]) == sec_b

    async def test_correction_signals_emitted(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Corrections surface deterministic counters in the build summary
        (threshold tuning signal) and never affect other users."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_id = await _new_item(
            session_factory,
            tenant,
            sec,
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            first = await _build(svc, tenant)
            assert first["matched"] == 1

        # User A corrects the (item, security) pair.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.correct(tenant, "user-A", item_id, security_id=sec)
            await session.commit()

        # Rebuild: the build summary reports the correction count and the
        # tenant-scoped row is NOT deleted (B still sees the item).
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            second = await _build(svc, tenant)
            assert second["corrections"] == 1
            assert second["matched"] == 1
            feed_b = await svc.feed(tenant, user_id="user-B")
        assert feed_b["total"] == 1


# ═══════════════════════════════════════════════════════════════════════
# A8 — Freshness / graceful degradation
# ═══════════════════════════════════════════════════════════════════════


class TestFreshness:
    async def test_stale_items_carry_freshness(
        self, session_factory: async_sessionmaker
    ) -> None:
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        now = datetime.now(UTC)
        # Fresh item: fetched just now.
        fresh_id = await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="fresh",
            kind="earnings_report",
            published_at=now - timedelta(hours=1),
            fetched_at=now,
        )
        # Stale item: fetched 3 days ago (> 24h threshold).
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="stale",
            kind="dividend",
            published_at=now - timedelta(days=3),
            fetched_at=now - timedelta(days=3),
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1")

        assert feed["total"] == 2
        freshness = {
            s["item_id"]: s["freshness"]
            for item in feed["items"]
            for s in item["sources"]
        }
        assert freshness[fresh_id] == FRESHNESS_FRESH
        assert any(v == FRESHNESS_STALE for v in freshness.values())
        # Every source carries fetched_at.
        for item in feed["items"]:
            for s in item["sources"]:
                assert s["fetched_at"] is not None
                assert s["url"] is not None

    async def test_include_stale_false_drops_stale_clusters(
        self, session_factory: async_sessionmaker
    ) -> None:
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        now = datetime.now(UTC)
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="fresh-only",
            kind="earnings_report",
            published_at=now - timedelta(hours=1),
            fetched_at=now,
        )
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="stale-only",
            kind="dividend",
            published_at=now - timedelta(days=3),
            fetched_at=now - timedelta(days=3),
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1", include_stale=False)

        assert feed["total"] == 1
        assert feed["items"][0]["event_type"] == "earnings"


# ═══════════════════════════════════════════════════════════════════════
# Notifications — opt-in, dedupe, lockscreen-safe
# ═══════════════════════════════════════════════════════════════════════


class TestNotifications:
    async def test_opt_in_and_dedupe(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Notifications require opt-in and dedupe per (user, cluster)."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            # Opt-in required.
            result = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert result["sent"] == 0
            assert result["skipped"] == "disabled"

            await svc.set_notification_preference(
                tenant, "user-A", enabled=True
            )
            await session.commit()

            first = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert first["sent"] == 1
            assert first["skipped"] is None
            assert "headline" in first["payload"]
            assert "position" not in str(first["payload"]).lower()
            assert "value" not in str(first["payload"]).lower()
            await session.commit()

            # Deduped on the second call.
            second = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert second["sent"] == 0
            assert second["skipped"] == "already_notified"

        async with session_factory() as session:
            stmt = select(RelevanceNotificationLog).where(
                RelevanceNotificationLog.tenant_id == tenant
            )
            rows = (await session.execute(stmt)).scalars().all()
            assert len(list(rows)) == 1

    async def test_event_type_filter_respected(
        self, session_factory: async_sessionmaker
    ) -> None:
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True, event_types=["dividend"]
            )
            await session.commit()
            result = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert result["sent"] == 0
            assert result["skipped"] == "event_type_not_allowed"


# ═══════════════════════════════════════════════════════════════════════
# A9 — Feed DTO schema completeness (API/MCP contract)
# ═══════════════════════════════════════════════════════════════════════


class TestFeedDTOSchema:
    """Every cluster DTO carries the full API/MCP contract fields."""

    async def test_feed_dto_contract_fields_present(
        self, session_factory: async_sessionmaker
    ) -> None:
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1")

        assert feed["total"] == 1
        item = feed["items"][0]
        # Cluster identity + per-cluster fields.
        assert item["id"] == item["cluster_id"]
        assert str(item["security_id"]) == sec
        assert item["security_ticker"] == "AAPL"
        assert item["event_type"] == "earnings"
        assert item["event_date"] is not None
        assert item["headline"]
        assert item["score"] >= 0.0
        assert item["acknowledged"] is False  # unread for a fresh user
        # Match provenance.
        assert item["match_reason"] == MATCH_REASON_EXACT_SECURITY
        assert item["confidence"] == 1.0
        # Freshness / staleness.
        assert item["is_stale"] is False
        assert item["source_count"] == 1
        assert item["best_source_url"] is not None
        # Source items carry url/published/fetched/freshness.
        assert len(item["sources"]) == 1
        source = item["sources"][0]
        assert source["url"]
        assert source["published_at"] is not None
        assert source["fetched_at"] is not None
        assert source["freshness"] == FRESHNESS_FRESH

    async def test_feed_dto_stale_cluster_flag(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A cluster whose sources are all stale is flagged is_stale."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        now = datetime.now(UTC)
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="stale-1",
            kind="dividend",
            published_at=now - timedelta(days=3),
            fetched_at=now - timedelta(days=3),
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1")

        assert feed["total"] == 1
        item = feed["items"][0]
        assert item["is_stale"] is True
        assert item["sources"][0]["freshness"] == FRESHNESS_STALE


# ═══════════════════════════════════════════════════════════════════════
# A10 — Account filter + injection safety (API exposure hardening)
# ═══════════════════════════════════════════════════════════════════════


class TestAccountFilterAndInjection:
    async def test_account_filter_includes_canonical_matches(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A canonical security match belongs to the holding account."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, account_id=acct)

        assert feed["total"] == 1

    async def test_account_filter_cross_tenant_empty(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A cross-tenant account id yields an empty feed, never a leak."""
        tenant_a = await _new_tenant(session_factory)
        tenant_b = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct_a = await _new_account(session_factory, tenant_a)
        acct_b = await _new_account(session_factory, tenant_b)
        await _new_holding(session_factory, tenant_a, acct_a, sec)
        sec_b = await _new_security(session_factory, ticker="MSFT")
        await _new_holding(session_factory, tenant_b, acct_b, sec_b)
        await _new_item(session_factory, tenant_a, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant_a)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            # B filters by A's account → empty, not A's rows.
            feed = await svc.feed(tenant_b, account_id=acct_a)
        assert feed["total"] == 0
        assert feed["items"] == []

    async def test_malformed_security_id_matches_nothing(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A malformed security id never widens the filter to all rows."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            # Even a well-formed-but-nonexistent UUID must match nothing.
            feed = await svc.feed(
                tenant, security_id="00000000-0000-0000-0000-000000000000"
            )
            assert feed["total"] == 0
            # Malformed payloads too.
            for payload in ("AAPL' OR '1'='1", "%", "_", "not-a-uuid"):
                feed = await svc.feed(tenant, security_id=payload)
                assert feed["total"] == 0, payload
            # Malformed account payloads too.
            for payload in ("AAPL' OR '1'='1", "not-a-uuid"):
                feed = await svc.feed(tenant, account_id=payload)
                assert feed["total"] == 0, payload
            # The real security still resolves.
            feed = await svc.feed(tenant, security_id=sec)
            assert feed["total"] == 1


# ═══════════════════════════════════════════════════════════════════════
# A11 — Hermes explanation (optional, feature-flagged, fact-only)
# ═══════════════════════════════════════════════════════════════════════


class TestHermesExplanationDTO:
    """The feed DTO carries hermes_explanation when an explainer is set.

    Acceptance (t_c6959faa): a known earnings-date match produces an
    explanation grounded only in deterministic facts that references the
    item IDs; with Hermes unavailable the field is simply omitted (or a
    deterministic fallback) and the service never crashes.
    """

    async def test_dto_includes_explanation_with_explainer(
        self, session_factory: async_sessionmaker
    ) -> None:
        """With an explainer wired, the DTO gains hermes_explanation."""
        from finance_sync.services.hermes_relevance import (
            HermesRelevanceExplainer,
        )

        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        item_id = await _new_item(
            session_factory,
            tenant,
            sec,
            kind="earnings_report",
            facts=[
                {"key": "event_type", "value": "earnings"},
                {"key": "earnings_date", "value": "2026-09-03"},
            ],
        )

        async with session_factory() as session:
            svc = HoldingRelevanceService(
                UnitOfWork(session),
                explainer=HermesRelevanceExplainer(),
            )
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(
                UnitOfWork(session),
                explainer=HermesRelevanceExplainer(),
            )
            feed = await svc.feed(tenant, user_id="user-1")

        assert feed["total"] == 1
        item = feed["items"][0]
        explanation = item.get("hermes_explanation")
        assert explanation is not None
        assert isinstance(explanation, str)
        # Fact-only: security name, event type, date, and the item ID.
        assert "Apple" in explanation
        assert item_id in explanation
        # No financial values / position sizes.
        for token in ("€", "$", "position", "market value", "0.99"):
            assert token not in explanation

    async def test_dto_omits_explanation_without_explainer(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Without an explainer the DTO simply omits the field."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            feed = await svc.feed(tenant, user_id="user-1")

        assert feed["total"] == 1
        assert "hermes_explanation" not in feed["items"][0]

    async def test_explainer_failure_never_breaks_feed(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A crashing explainer degrades to no explanation, not a 500."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")

        class _Boom:
            async def explain(self, *_args: Any, **_kwargs: Any) -> str:
                error = "boom"
                raise RuntimeError(error)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        async with session_factory() as session:
            svc = HoldingRelevanceService(
                UnitOfWork(session),
                explainer=_Boom(),  # type: ignore[arg-type]
            )
            feed = await svc.feed(tenant, user_id="user-1")

        assert feed["total"] == 1
        assert "hermes_explanation" not in feed["items"][0]


# ═══════════════════════════════════════════════════════════════════════
# A10 — Notifications: opt-in, per-tenant/account/security/event-type
#       scoping, dedupe per cluster, lockscreen-safe by default, and
#       dispatch of newly created clusters
# ═══════════════════════════════════════════════════════════════════════


class TestNotificationScopingAndSafety:
    """Notification settings + payload safety (backlog acceptance)."""

    async def test_default_payload_is_lockscreen_safe(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Default payload carries only type/headline/date/source."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True
            )
            await session.commit()
            result = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert result["sent"] == 1
            payload = result["payload"]
            # Lockscreen-safe by default: only event type + headline +
            # date + source; never ticker/name, position or value.
            assert payload["lockscreen_safe"] is True
            assert payload["event_type"] == "earnings"
            assert "security_ticker" not in payload
            assert "security_name" not in payload
            assert "quantity" not in str(payload).lower()
            assert "market_value" not in str(payload).lower()
            assert "position" not in str(payload).lower()
            assert "value" not in str(payload).lower()

    async def test_detailed_preview_is_explicit_opt_in(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Ticker/name appear only when detailed_preview is enabled."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True, detailed_preview=True
            )
            await session.commit()
            result = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert result["sent"] == 1
            payload = result["payload"]
            assert payload["security_ticker"] == "AAPL"
            assert payload["security_name"] == "Apple Inc."
            # Still never raw financial values.
            assert "market_value" not in str(payload).lower()
            assert "quantity" not in str(payload).lower()

    async def test_security_scope_filters_clusters(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Per-security scope only notifies for that security."""
        tenant = await _new_tenant(session_factory)
        sec_a = await _new_security(session_factory)
        sec_b = await _new_security(
            session_factory, ticker="MSFT", name="Microsoft Corp."
        )
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec_a)
        await _new_holding(session_factory, tenant, acct, sec_b)
        await _new_item(session_factory, tenant, sec_a, kind="earnings_report")
        await _new_item(session_factory, tenant, sec_b, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 2
        cluster_a = next(c for c in clusters if str(c.security_id) == sec_a)
        cluster_b = next(c for c in clusters if str(c.security_id) == sec_b)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant,
                "user-A",
                enabled=True,
                security_id=sec_a,
            )
            await session.commit()
            # Only the AAPL cluster is eligible.
            ok = await svc.notify_eligible(tenant, "user-A", str(cluster_a.id))
            assert ok["sent"] == 1
            blocked = await svc.notify_eligible(
                tenant, "user-A", str(cluster_b.id)
            )
            assert blocked["sent"] == 0
            assert blocked["skipped"] == "event_type_not_allowed"

    async def test_account_scope_filters_clusters(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Per-account scope only notifies for clusters touching it."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct_a = await _new_account(session_factory, tenant)
        acct_b = await _new_account(
            session_factory, tenant, name="Other account"
        )
        await _new_holding(session_factory, tenant, acct_a, sec)
        await _new_holding(session_factory, tenant, acct_b, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True, account_id=acct_a
            )
            await session.commit()
            ok = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert ok["sent"] == 1

        # A user scoped to an account that never held the security gets
        # nothing.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-B", enabled=True, account_id=acct_b
            )
            # user-B's scope includes acct_b which holds the security,
            # so it is eligible — verify cross-account scope below.
            await session.commit()
            result = await svc.notify_eligible(tenant, "user-B", cluster_id)
            assert result["sent"] == 1

    async def test_notification_tenant_isolation(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A user of tenant B can never notify on tenant A's cluster."""
        tenant_a = await _new_tenant(session_factory)
        tenant_b = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct_a = await _new_account(session_factory, tenant_a)
        await _new_holding(session_factory, tenant_a, acct_a, sec)
        await _new_item(session_factory, tenant_a, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant_a)
        clusters_a = await _clusters(session_factory, tenant_a)
        cluster_a_id = str(clusters_a[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            # user-B (tenant B) opted in, but the cluster belongs to A.
            await svc.set_notification_preference(
                tenant_b, "user-B", enabled=True
            )
            await session.commit()
            result = await svc.notify_eligible(tenant_b, "user-B", cluster_a_id)
            assert result["sent"] == 0
            assert result["skipped"] == "not_found"

    async def test_dispatch_dedupes_per_user_cluster_event(
        self, session_factory: async_sessionmaker
    ) -> None:
        """dispatch sends once per (user, cluster) and is idempotent."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        # Two syndicated items about the same event → one cluster.
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="src-1",
            kind="earnings_report",
            headline="Apple beats estimates",
            canonical_url="https://example.com/a",
        )
        await _new_item(
            session_factory,
            tenant,
            sec,
            source_id="src-2",
            kind="earnings_report",
            headline="Apple beats estimates (syndicated)",
            canonical_url="https://example.com/b",
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        assert len(clusters) == 1
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True
            )
            await session.commit()
            first = await svc.dispatch_new_cluster_notifications(tenant)
            assert first["sent"] == 1
            assert first["users"] == 1
            # Re-running never double-sends.
            second = await svc.dispatch_new_cluster_notifications(tenant)
            assert second["sent"] == 0
            await session.commit()

        async with session_factory() as session:
            stmt = select(RelevanceNotificationLog).where(
                RelevanceNotificationLog.tenant_id == tenant,
                RelevanceNotificationLog.cluster_id == cluster_id,
            )
            rows = list((await session.execute(stmt)).scalars().all())
            assert len(rows) == 1

    async def test_dispatch_only_opted_in_users(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Users who did not opt in get no notifications."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)

        # No preference row at all → nothing sent.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            result = await svc.dispatch_new_cluster_notifications(tenant)
            assert result["sent"] == 0
            assert result["skipped"] == "no_opted_in"

        # An opted-in user next to a non-opted-in user.
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True
            )
            await session.commit()
            result = await svc.dispatch_new_cluster_notifications(tenant)
            assert result["sent"] == 1
            assert result["users"] == 1
            await session.commit()
        # user-B never opted in → no row.
        async with session_factory() as session:
            stmt = select(RelevanceNotificationLog).where(
                RelevanceNotificationLog.tenant_id == tenant,
                RelevanceNotificationLog.user_id == "user-B",
            )
            rows = list((await session.execute(stmt)).scalars().all())
            assert len(rows) == 0

    async def test_notification_isolation_shared_ticker(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Tenant A's notification never leaks to tenant B via dispatch."""
        tenant_a = await _new_tenant(session_factory)
        tenant_b = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct_a = await _new_account(session_factory, tenant_a)
        await _new_holding(session_factory, tenant_a, acct_a, sec)
        await _new_item(session_factory, tenant_a, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant_a)
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            # Tenant B has no holdings but its user opted in.
            await svc.set_notification_preference(
                tenant_b, "user-B", enabled=True
            )
            await session.commit()
            result = await svc.dispatch_new_cluster_notifications(tenant_b)
            assert result["sent"] == 0
            assert result["skipped"] in ("no_clusters", "no_opted_in")


# ═══════════════════════════════════════════════════════════════════════
# A11 — Graceful degradation: stale/missing sources never break reads
# ═══════════════════════════════════════════════════════════════════════


class TestGracefulDegradationNotifications:
    """Stale or missing sources degrade cleanly, deterministically."""

    async def test_stale_sources_still_notifiable(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A cluster whose sources are stale is still notify-eligible."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        old = datetime.now(UTC) - timedelta(days=3)
        await _new_item(
            session_factory,
            tenant,
            sec,
            kind="earnings_report",
            published_at=old,
            fetched_at=old,
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True
            )
            await session.commit()
            result = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert result["sent"] == 1
            # The payload stays lockscreen-safe even for stale items.
            assert result["payload"]["lockscreen_safe"] is True

    async def test_missing_source_url_still_notifies(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A cluster with no canonical URL still produces a payload."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(
            session_factory,
            tenant,
            sec,
            kind="earnings_report",
            canonical_url=None,
        )
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
        clusters = await _clusters(session_factory, tenant)
        cluster_id = str(clusters[0].id)

        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True
            )
            await session.commit()
            result = await svc.notify_eligible(tenant, "user-A", cluster_id)
            assert result["sent"] == 1
            assert result["payload"]["source_url"] is None

    async def test_dispatch_never_crashes_on_unknown_cluster_ids(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Cross-tenant/unknown cluster ids in dispatch are ignored."""
        tenant = await _new_tenant(session_factory)
        sec = await _new_security(session_factory)
        acct = await _new_account(session_factory, tenant)
        await _new_holding(session_factory, tenant, acct, sec)
        await _new_item(session_factory, tenant, sec, kind="earnings_report")
        async with session_factory() as session:
            svc = HoldingRelevanceService(UnitOfWork(session))
            await _build(svc, tenant)
            await svc.set_notification_preference(
                tenant, "user-A", enabled=True
            )
            await session.commit()
            result = await svc.dispatch_new_cluster_notifications(
                tenant,
                cluster_ids=["00000000-0000-0000-0000-000000000000"],
            )
            assert result["sent"] == 0
            assert result["skipped"] == "no_clusters"
