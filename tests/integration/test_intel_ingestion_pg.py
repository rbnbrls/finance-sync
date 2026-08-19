"""Integration tests for the market-intelligence ingestion pipeline.

Exercises the incremental-ingestion acceptance criteria against **real**
PostgreSQL (FK constraints, JSONB, UUID pk, unique constraints — none of
which the aiosqlite unit suite can prove):

* idempotent reruns never create duplicates (unique constraints hold);
* upsert semantics: a changed item with the same (provider, source_id)
  updates in place; a syndicated duplicate (same content_hash, other
  provider) never mutates the first provider's row;
* a provider outage never deletes previously valid observations — only
  the freshness rule soft-flags them stale;
* licensing policy: no disallowed full text is stored;
* ambiguous identity matches land in the review queue, never silently
  linked (FK to securities is real here).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from finance_sync.db.uow import UnitOfWork
from finance_sync.intel.enums import (
    IntelItemKind,
    IntelLicenseClass,
    IntelResolutionStatus,
)
from finance_sync.intel.hashing import content_hash
from finance_sync.intel.models import IntelItem, IntelStructuredFact
from finance_sync.intel.service import IntelIngestionService
from finance_sync.models import Tenant
from finance_sync.models.enums import SecurityType
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.market_intelligence_review_queue import (
    MarketIntelligenceReviewQueue,
)
from finance_sync.models.security import Security

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────


async def _create_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str | None = None,
) -> Tenant:
    slug = slug or f"t-{uuid4().hex[:12]}"
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.tenants.add(Tenant(slug=slug, name="Intel Test"))


async def _create_security(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ticker: str,
    isin: str | None = None,
    name: str = "Test Corp",
) -> Security:
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.securities.add(
            Security(
                isin=isin,
                ticker=ticker,
                name=name,
                security_type=SecurityType.STOCK,
                currency_code="USD",
            )
        )


@pytest.fixture
async def tenant(session_factory: async_sessionmaker[AsyncSession]) -> Tenant:
    return await _create_tenant(session_factory)


class _FakeResolver:
    """SecurityResolver double with a configurable match table."""

    def __init__(self, matches: dict[Any, Any]) -> None:
        self.matches = matches

    async def resolve_by_isin(self, isin: str) -> Any:
        return self.matches.get(("isin", isin))

    async def resolve_by_figi(self, figi: str) -> Any:
        return self.matches.get(("figi", figi))

    async def resolve_by_ticker(self, ticker: str) -> Any:
        return self.matches.get(("ticker", ticker))


def _resolved(security_id: str, confidence: str = "exact") -> Any:
    from finance_sync.enrichment.models import ResolvedSecurity

    return ResolvedSecurity(
        security_id=security_id,
        isin="US0378331005",
        figi=None,
        ticker="AAPL",
        name="Apple Inc.",
        currency_code="USD",
        confidence=confidence,
        source="local_db",
    )


def _item(
    *,
    provider: str = "openbb",
    source_id: str | None = None,
    headline: str = "AAPL beats estimates",
    summary: str | None = "Short summary",
    body: str | None = None,
    license_class: IntelLicenseClass = IntelLicenseClass.FREE_ACCESS,
    license_text: str | None = None,
    identifiers: dict[str, str] | None = None,
    store_full_text: bool = False,
    store_summary: bool = True,
) -> IntelItem:
    now = datetime.now(UTC)
    sid = source_id or str(uuid4())
    return IntelItem(
        provider=provider,
        source_id=sid,
        canonical_url=f"https://example.com/{sid}",
        kind=IntelItemKind.NEWS_ARTICLE,
        published_at=now,
        fetched_at=now,
        language="en",
        license_class=license_class,
        license_text=license_text,
        content_hash=content_hash(
            {"provider": provider, "source_id": sid, "headline": headline}
        ),
        headline=headline,
        summary=summary,
        body=body,
        store_full_text=store_full_text,
        store_summary=store_summary,
        identifiers=identifiers or {},
        facts=[
            IntelStructuredFact(key="eps_estimate", value="1.25", unit="USD")
        ],
    )


async def _ingest(
    session_factory: async_sessionmaker[AsyncSession],
    resolver: Any,
    tenant_id: str,
    provider: str,
    items: list[IntelItem],
) -> dict[str, int]:
    async with session_factory() as session:
        uow = UnitOfWork(session)
        service = IntelIngestionService(uow, resolver)
        result = await service.ingest_items(tenant_id, provider, items)
        await uow.commit()
    return result


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    model: Any,
    **filters: Any,
) -> int:
    async with session_factory() as session:
        stmt = select(model)
        for col, val in filters.items():
            stmt = stmt.where(getattr(model, col) == val)
        rows = (await session.execute(stmt)).scalars().all()
        return len(list(rows))


# ═══════════════════════════════════════════════════════════════════════
# Idempotent reruns + dedup on real PG
# ═══════════════════════════════════════════════════════════════════════


class TestIdempotentRerunPG:
    async def test_rerun_creates_no_duplicates(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """Ingesting the same batch twice → exactly one row per item."""
        resolver = _FakeResolver({})
        items = [
            _item(source_id=f"rerun-{i}", headline=f"Story {i}")
            for i in range(5)
        ]
        r1 = await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", items
        )
        assert r1["ingested"] == 5
        r2 = await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", items
        )
        assert r2["duplicates"] == 5
        assert r2["ingested"] == 0
        assert r2["updated"] == 0
        assert (
            await _count(
                session_factory,
                MarketIntelligenceItem,
                tenant_id=str(tenant.id),
                provider="openbb",
            )
            == 5
        )

    async def test_tenant_isolation_on_pg(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """Two tenants ingesting the same syndicated item each get one row."""
        tenant_b = await _create_tenant(
            session_factory, slug=f"t-{uuid4().hex[:12]}"
        )
        resolver = _FakeResolver({})
        item = _item(source_id="press-1", headline="Shared press release")

        await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", [item]
        )
        await _ingest(
            session_factory, resolver, str(tenant_b.id), "openbb", [item]
        )

        assert (
            await _count(
                session_factory,
                MarketIntelligenceItem,
                tenant_id=str(tenant.id),
                source_id="press-1",
            )
            == 1
        )
        assert (
            await _count(
                session_factory,
                MarketIntelligenceItem,
                tenant_id=str(tenant_b.id),
                source_id="press-1",
            )
            == 1
        )


# ═══════════════════════════════════════════════════════════════════════
# Upsert semantics on real PG
# ═══════════════════════════════════════════════════════════════════════


class TestUpsertPG:
    async def test_changed_source_updates_in_place(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """Same (provider, source_id), changed content → update, one row."""
        resolver = _FakeResolver({})
        v1 = _item(source_id="upd-pg", headline="v1 headline")
        v2 = _item(source_id="upd-pg", headline="v2 headline (revised)")

        r1 = await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", [v1]
        )
        assert r1["ingested"] == 1
        r2 = await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", [v2]
        )
        assert r2["updated"] == 1
        assert r2["duplicates"] == 0

        assert (
            await _count(
                session_factory,
                MarketIntelligenceItem,
                tenant_id=str(tenant.id),
                provider="openbb",
            )
            == 1
        )
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MarketIntelligenceItem).where(
                            MarketIntelligenceItem.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(list(rows)) == 1
            row = next(iter(rows))
            assert row.headline == "v2 headline (revised)"
            assert row.source_id == "upd-pg"

    async def test_syndicated_duplicate_keeps_first_provider(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """Same content_hash from another provider → duplicate, no update."""
        resolver = _FakeResolver({})
        now = datetime.now(UTC)
        shared = {"provider": "openbb", "source_id": "synd-pg", "headline": "S"}
        item_a = _item(source_id="synd-pg", headline="S")
        item_b = IntelItem(
            provider="sec",
            source_id="synd-pg-sec",
            canonical_url="https://example.com/synd-pg-sec",
            kind=IntelItemKind.NEWS_ARTICLE,
            published_at=now,
            fetched_at=now,
            language="en",
            license_class=IntelLicenseClass.FREE_ACCESS,
            content_hash=content_hash(shared),
            headline="S (sec variant)",
            store_full_text=False,
            store_summary=True,
        )

        await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", [item_a]
        )
        r2 = await _ingest(
            session_factory, resolver, str(tenant.id), "sec", [item_b]
        )
        assert r2["duplicates"] == 1
        assert r2["updated"] == 0

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MarketIntelligenceItem).where(
                            MarketIntelligenceItem.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(list(rows)) == 1
            row = next(iter(rows))
            assert row.provider == "openbb"
            assert row.headline == "S"


# ═══════════════════════════════════════════════════════════════════════
# Provider outage: never deletes; freshness soft-marks stale
# ═══════════════════════════════════════════════════════════════════════


class TestOutagePG:
    async def test_outage_keeps_observations_and_marks_stale(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """An outage keeps rows; the freshness rule soft-flags them stale."""
        resolver = _FakeResolver({})
        now = datetime.now(UTC)
        old = _item(provider="sec", source_id="pg-old", headline="Old filing")
        old.fetched_at = now - timedelta(days=30)
        await _ingest(session_factory, resolver, str(tenant.id), "sec", [old])

        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, resolver)
            marked = await service.mark_stale(
                str(tenant.id),
                "sec",
                older_than=now - timedelta(hours=6),
            )
            await uow.commit()
            assert marked == 1

        # The observation is still there, still queryable, soft-flagged.
        assert (
            await _count(
                session_factory,
                MarketIntelligenceItem,
                tenant_id=str(tenant.id),
                source_id="pg-old",
            )
            == 1
        )
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MarketIntelligenceItem).where(
                            MarketIntelligenceItem.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            row = next(iter(rows))
            assert row.is_stale is True
            assert row.headline == "Old filing"  # content intact


# ═══════════════════════════════════════════════════════════════════════
# Licensing policy on real PG: no disallowed full text stored
# ═══════════════════════════════════════════════════════════════════════


class TestLicensingPG:
    async def test_restricted_license_never_stores_body(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """A restricted license persists no body; metadata + link remain."""
        resolver = _FakeResolver({})
        from finance_sync.intel.service import apply_licensing_policy

        item = _item(
            source_id="lic-pg",
            license_class=IntelLicenseClass.FREE_ACCESS,
            license_text="copyright (c) 2026",
            summary="Allowed snippet",
            body="FULL ARTICLE BODY THAT MUST NEVER BE STORED",
            store_full_text=True,
        )
        cleaned = apply_licensing_policy(item)
        assert cleaned.body is None
        assert cleaned.license_class == IntelLicenseClass.SUBSCRIBER_ONLY

        await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", [cleaned]
        )
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MarketIntelligenceItem).where(
                            MarketIntelligenceItem.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            row = next(iter(rows))
            assert row.body is None
            assert row.summary == "Allowed snippet"
            assert row.canonical_url is not None
            assert row.license_class == IntelLicenseClass.SUBSCRIBER_ONLY.value


# ═══════════════════════════════════════════════════════════════════════
# Identity ambiguity on real PG: queued, never silently linked
# ═══════════════════════════════════════════════════════════════════════


class TestIdentityPG:
    async def test_ambiguous_match_queued_not_linked(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """Ambiguous identifiers → review queue, no security link."""
        sec_a = await _create_security(
            session_factory, ticker="NOK", isin="US6541061031", name="Nokia ADR"
        )
        sec_b = await _create_security(
            session_factory,
            ticker="NOKIA",
            isin="FI0009000681",
            name="Nokia Oyj",
        )
        resolver = _FakeResolver(
            {
                ("ticker", "NOK"): _resolved(str(sec_a.id), "ticker_only"),
                ("isin", "US6541061031"): _resolved(str(sec_b.id), "exact"),
            }
        )
        item = _item(
            source_id="amb-pg",
            headline="NOK surges after results",
            identifiers={"ticker": "NOK", "isin": "US6541061031"},
        )

        r = await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", [item]
        )
        assert r["review_required"] == 1

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MarketIntelligenceItem).where(
                            MarketIntelligenceItem.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            row = next(iter(rows))
            # Never silently linked.
            assert row.security_id is None
            assert (
                row.resolution_status == IntelResolutionStatus.AMBIGUOUS.value
            )
            assert row.review_required is True

            entries = (
                (
                    await session.execute(
                        select(MarketIntelligenceReviewQueue).where(
                            MarketIntelligenceReviewQueue.tenant_id
                            == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(list(entries)) == 1
            entry = next(iter(entries))
            assert entry.resolution_status == "pending"
            assert entry.candidate_identifiers is not None
            assert len(entry.candidate_identifiers) == 2

    async def test_unambiguous_match_links_security(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """A single distinct, high-confidence match links the security."""
        sec = await _create_security(
            session_factory,
            ticker="AAPL",
            isin="US0378331005",
            name="Apple Inc.",
        )
        resolver = _FakeResolver(
            {
                ("ticker", "AAPL"): _resolved(str(sec.id), "exact"),
                ("isin", "US0378331005"): _resolved(str(sec.id), "exact"),
            }
        )
        item = _item(
            source_id="res-pg",
            headline="Apple reports",
            identifiers={"ticker": "AAPL", "isin": "US0378331005"},
        )

        r = await _ingest(
            session_factory, resolver, str(tenant.id), "openbb", [item]
        )
        assert r["review_required"] == 0

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MarketIntelligenceItem).where(
                            MarketIntelligenceItem.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            row = next(iter(rows))
            assert str(row.security_id) == str(sec.id)
            assert row.resolution_status == IntelResolutionStatus.RESOLVED.value
            assert row.review_required is False


# ═══════════════════════════════════════════════════════════════════════
# Read contract: stale flag exposed, tenant-scoped, never credentials
# ═══════════════════════════════════════════════════════════════════════


class TestReadContractPG:
    async def test_stale_filter_exposed_tenant_scoped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant: Tenant,
    ) -> None:
        """The read service exposes stale items only to their tenant."""
        resolver = _FakeResolver({})
        now = datetime.now(UTC)
        item = _item(provider="sec", source_id="stale-pg", headline="Old")
        item.fetched_at = now - timedelta(days=20)
        await _ingest(session_factory, resolver, str(tenant.id), "sec", [item])
        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, resolver)
            await service.mark_stale(
                str(tenant.id),
                "sec",
                older_than=now - timedelta(hours=1),
            )
            await uow.commit()

        from finance_sync.services.market_intelligence_read import (
            MarketIntelligenceReadService,
        )

        async with session_factory() as session:
            read = MarketIntelligenceReadService(session)
            stale = await read.list_items(
                str(tenant.id), provider="sec", is_stale=True
            )
            assert stale.total == 1
            assert stale.items[0].is_stale is True
            assert stale.items[0].body is None  # restricted: never served

            fresh = await read.list_items(
                str(tenant.id), provider="sec", is_stale=False
            )
            assert fresh.total == 0
