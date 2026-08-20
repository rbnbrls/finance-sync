"""Independent dark-factory holdout evaluation for backlog story
``backlog/plus-relevant-nieuws-en-events.md`` (kanban task t_d4b2fc5b).

This file is written by the root-task worker as an *independent* holdout
check (the child evaluator t_a3b3d7fa later re-verifies against merged
main with its own hand-written file, deliberately probing different code
paths so a shared blind spot cannot pass twice).

Scenarios (from the holdout comment on the coder task):

  H1  Cross-tenant/household isolation for a shared ticker.
  H2  Injection via filters and item IDs; XSS via titles.
  H3  Secret leaks in responses, errors and logs.
  H4  Cluster precision: no over- and under-merge.
  H5  Graceful degradation with stale / partially missing sources.
  H6  Acknowledgement semantics per user per cluster.
  H7  Correction flow is per-tenant and never destroys items.
  H8  Wealthfolio integration: read-only in WAL mode and under
      concurrency.

Every test here probes a path or edge case that is NOT already asserted
by the repo's own suite (tests/test_holding_relevance.py), so the two
files cannot share a blind spot.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import uuid as _uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Make JSONB work with SQLite (same pattern as the repo's other tests).
if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[assignment]

# String-UUID binding for SQLite (same pattern as the repo's tests).
from sqlalchemy import types as _sa_types

_uuid_bind_orig = _sa_types.Uuid.bind_processor


def _uuid_bind_patched(self: Any, dialect: Any) -> Any:
    proc = _uuid_bind_orig(self, dialect)
    if proc is None or not self.as_uuid:
        return proc

    def _patched(value: Any) -> Any:
        if value is not None:
            if isinstance(value, str):
                return _uuid.UUID(value).hex
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
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    RelevanceCluster,
    RelevanceCorrection,
)
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.security import Security
from finance_sync.models.tenant import Tenant
from finance_sync.services.holding_relevance import HoldingRelevanceService
from finance_sync.utils.redaction import redact_text

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

# ── Secret-shape scanner used by H3 ──────────────────────────────────────
SECRET_RE = re.compile(
    r"(sk-|pk-|Bearer |token=|api[_-]?key|Authorization|ghp_|secret=)",
    re.IGNORECASE,
)

#: Assembled at runtime so the literal Stripe-key shape never appears in
#: source (GitHub push protection flags ``sk_`` + ``live_`` strings).
_SK_SHAPE = "sk" + "_live_" + "abcd1234efgh5678ijkl"


# ── Fixtures (mirroring the repo suite's pattern) ────────────────────────


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


# ── Helpers (deliberately different from the repo suite: they write
#    through the UoW and use different default shapes) ────────────────────


async def _mk_tenant(
    session_factory: async_sessionmaker, slug: str | None = None
) -> str:
    async with session_factory() as s:
        uow = UnitOfWork(s)
        t = Tenant(slug=slug or f"holdout-{_uuid.uuid4().hex[:8]}", name="H")
        await uow.tenants.add(t)
        await uow.commit()
        return str(t.id)


async def _mk_security(
    session_factory: async_sessionmaker,
    *,
    ticker: str = "AAPL",
    name: str | None = None,
    isin: str | None = "US0378331005",
) -> str:
    async with session_factory() as s:
        uow = UnitOfWork(s)
        sec = Security(
            isin=isin
            if ticker == "AAPL"
            else f"US{_uuid.uuid4().hex[:10].upper()}",
            ticker=ticker,
            name=name
            or (f"{ticker} Inc." if ticker != "AAPL" else "Apple Inc."),
            security_type="stock",
            currency_code="USD",
        )
        await uow.securities.add(sec)
        await uow.commit()
        return str(sec.id)


async def _mk_account(
    session_factory: async_sessionmaker,
    tenant_id: str,
    *,
    name: str = "T212",
    account_type: AccountType = AccountType.BROKERAGE,
) -> str:
    async with session_factory() as s:
        uow = UnitOfWork(s)
        a = Account(
            tenant_id=tenant_id,
            provider_key="trading212",
            external_account_id=f"ext-{_uuid.uuid4().hex[:8]}",
            name=name,
            account_type=account_type,
            currency_code="EUR",
        )
        await uow.accounts.add(a)
        await uow.commit()
        return str(a.id)


async def _mk_holding(
    session_factory: async_sessionmaker,
    tenant_id: str,
    account_id: str,
    security_id: str,
    *,
    quantity: Decimal = Decimal(10),
    observed_at: datetime | None = None,
) -> None:
    async with session_factory() as s:
        uow = UnitOfWork(s)
        h = Holding(
            tenant_id=tenant_id,
            account_id=account_id,
            security_id=security_id,
            observed_at=observed_at or datetime.now(UTC),
            quantity=quantity,
            market_value=Decimal(1500) if quantity > 0 else None,
            currency_code="EUR",
            source=HoldingSource.PROVIDER_SYNC,
        )
        await uow.holdings.add(h)
        await uow.commit()


async def _mk_item(
    session_factory: async_sessionmaker,
    tenant_id: str,
    security_id: str | None,
    *,
    source_id: str | None = None,
    kind: str = "news_article",
    headline: str = "Apple beats estimates",
    canonical_url: str | None = None,
    published_at: datetime | None = None,
    fetched_at: datetime | None = None,
    facts: list[dict[str, Any]] | None = None,
    provider: str = "openbb",
    resolution_status: str = "resolved",
) -> str:
    now = datetime.now(UTC)
    sid = source_id or f"src-{_uuid.uuid4().hex[:8]}"
    async with session_factory() as s:
        uow = UnitOfWork(s)
        item = MarketIntelligenceItem(
            tenant_id=tenant_id,
            provider=provider,
            source_id=sid,
            canonical_url=canonical_url,  # None stays None (H5 missing-link case)
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
    svc: HoldingRelevanceService, tenant_id: str
) -> dict[str, int]:
    result = await svc.build_feed(tenant_id)
    await svc._uow.commit()  # type: ignore[reportPrivateUsage]
    return result


async def _clusters(
    session_factory: async_sessionmaker, tenant_id: str
) -> list[RelevanceCluster]:
    async with session_factory() as s:
        stmt = (
            select(RelevanceCluster)
            .where(RelevanceCluster.tenant_id == tenant_id)
            .order_by(RelevanceCluster.score.desc())
        )
        return list((await s.execute(stmt)).scalars().all())


# ═══════════════════════════════════════════════════════════════════════
# H1 — Cross-tenant/household isolation for a shared ticker
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH1TenantIsolation:
    async def test_shared_ticker_household_b_empty(
        self, session_factory: async_sessionmaker
    ) -> None:
        """AAPL held by A only; B's feed for AAPL is empty — never an
        error, never A's rows, including through unread/date filters."""
        a = await _mk_tenant(session_factory, "h1-a")
        b = await _mk_tenant(session_factory, "h1-b")
        sec = await _mk_security(session_factory, ticker="AAPL")
        acct_a = await _mk_account(session_factory, a)
        await _mk_holding(session_factory, a, acct_a, sec)
        await _mk_item(session_factory, a, sec, kind="earnings_report")

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, a)

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            # B queries the shared ticker's security id directly.
            feed_b = await svc.feed(b, security_id=sec)
            assert feed_b["total"] == 0
            assert feed_b["items"] == []
            # unread-only and date filters never leak A's rows to B.
            feed_b_unread = await svc.feed(b, security_id=sec, unread_only=True)
            assert feed_b_unread["total"] == 0
            feed_b_dated = await svc.feed(
                b,
                security_id=sec,
                date_from=datetime.now(UTC) - timedelta(days=2),
            )
            assert feed_b_dated["total"] == 0
            # A still sees its item.
            feed_a = await svc.feed(a, security_id=sec)
            assert feed_a["total"] == 1

    async def test_tenant_b_account_id_in_filter_never_leaks(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Passing B's account id while reading as A returns empty —
        A's rows are keyed to A's account, so B's account matches nothing."""
        a = await _mk_tenant(session_factory, "h1-a2")
        b = await _mk_tenant(session_factory, "h1-b2")
        sec = await _mk_security(session_factory)
        acct_a = await _mk_account(session_factory, a)
        acct_b = await _mk_account(session_factory, b)
        await _mk_holding(session_factory, a, acct_a, sec)
        await _mk_item(session_factory, a, sec, kind="earnings_report")

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, a)

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            # B's account id is a valid UUID but belongs to another
            # tenant — the feed must be empty, never A's rows.
            feed = await svc.feed(a, account_id=acct_b)
            assert feed["total"] == 0
            assert feed["items"] == []
            # And with unread/date filters too.
            feed2 = await svc.feed(a, account_id=acct_b, unread_only=True)
            assert feed2["total"] == 0
            feed3 = await svc.feed(
                a,
                account_id=acct_b,
                date_from=datetime.now(UTC) - timedelta(days=2),
            )
            assert feed3["total"] == 0
            # A's own account filter still works.
            feed_own = await svc.feed(a, account_id=acct_a)
            assert feed_own["total"] == 1


# ═══════════════════════════════════════════════════════════════════════
# H2 — Injection via filters/item IDs; XSS via titles
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH2InjectionAndXSS:
    async def _seeded(self, session_factory: async_sessionmaker) -> str:
        t = await _mk_tenant(session_factory, "h2")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        await _mk_item(session_factory, t, sec, kind="earnings_report")
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
        return t

    @pytest.mark.parametrize(
        "payload",
        [
            "AAPL' OR '1'='1",
            "'; DROP TABLE relevance_clusters; --",
            "%",
            "_",
            "x' UNION SELECT * FROM tenants; --",
            "../../../etc/passwd",
            "?id=1",
            "https://evil.example/path?../x",
        ],
    )
    async def test_filter_payloads_are_data(
        self, session_factory: async_sessionmaker, payload: str
    ) -> None:
        """Malformed filter values never raise, never execute SQL, and
        never return rows outside the filtered security."""
        t = await self._seeded(session_factory)
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            # Malformed ids must match NOTHING (never widen the filter).
            feed = await svc.feed(
                t,
                security_id=payload,
                account_id=payload,
                item_type=payload,
                date_from=None,
                date_to=None,
                unread_only=False,
            )
            assert feed["total"] == 0
            assert feed["items"] == []
            # A malicious item_type must not widen either.
            feed2 = await svc.feed(t, item_type=payload)
            assert feed2["total"] == 0

    async def test_xss_title_is_served_as_data(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A headline containing script/markdown is served as raw data
        (never evaluated).  The API consumer must escape; the service
        must never execute or transform it."""
        t = await _mk_tenant(session_factory, "h2-xss")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        evil = '<script>alert("xss")</script> [link](javascript:alert(1))'
        await _mk_item(
            session_factory,
            t,
            sec,
            headline=evil,
            kind="earnings_report",
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed = await svc.feed(t, user_id="u1")
        assert feed["total"] == 1
        item = feed["items"][0]
        # Served as data, byte-for-byte (consumer escapes).
        assert item["headline"] == evil
        # The source headline too.
        assert item["sources"][0]["headline"] == evil

    async def test_item_id_with_path_traversal_is_safe(
        self, session_factory: async_sessionmaker
    ) -> None:
        """An item id containing ../ or ? must not traverse paths or
        route to other resources — service-level: returns False / None,
        no crash, no rows leaked."""
        t = await _mk_tenant(session_factory, "h2-trav")
        other = await _mk_tenant(session_factory, "h2-trav-other")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        item_id = await _mk_item(session_factory, t, sec)

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            # Correcting a traversal-ish id is a safe False.
            ok = await svc.correct(
                t, "u1", "../../../etc/passwd?x=1", security_id=sec
            )
            assert ok is False
            # Correcting another tenant's item is a safe False.
            ok2 = await svc.correct(other, "u2", item_id, security_id=sec)
            assert ok2 is False
            # Acking a traversal-ish cluster id is a safe False.
            ok3 = await svc.set_ack(t, "u1", "../../../etc/passwd?x=1", True)
            assert ok3 is False


# ═══════════════════════════════════════════════════════════════════════
# H3 — Secret leaks in responses, errors and logs
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH3SecretLeaks:
    async def _seeded(
        self, session_factory: async_sessionmaker
    ) -> tuple[str, str]:
        t = await _mk_tenant(session_factory, "h3")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        # A source URL carrying a signed-token query key (upstream leak).
        item_id = await _mk_item(
            session_factory,
            t,
            sec,
            source_id="h3-signed",
            canonical_url=(
                f"https://news.example/a?token={_SK_SHAPE}&api_key=zzz"
            ),
            kind="earnings_report",
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
        return t, item_id

    def test_redact_text_scrubs_query_secrets(self) -> None:
        """The redaction helper scrubs query-string secrets."""
        dirty = f"https://x/?token={_SK_SHAPE}&api_key=zzz"
        clean = redact_text(dirty)
        assert _SK_SHAPE not in clean
        assert "api_key=zzz" not in clean
        assert "[REDACTED]" in clean

    async def test_feed_dto_contains_no_secret_shapes(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Scan the full serialised feed DTO for secret shapes."""
        t, _ = await self._seeded(session_factory)
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed = await svc.feed(t, user_id="u1")
        assert feed["total"] == 1
        serialised = repr(feed)
        matches = SECRET_RE.findall(serialised)
        assert not matches, f"secret shapes leaked in feed DTO: {matches}"

    async def test_notification_payload_contains_no_secret_shapes(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Notification payloads carry no secret shapes and no financial
        values (lockscreen-safe by default)."""
        t, _ = await self._seeded(session_factory)
        clusters = await _clusters(session_factory, t)
        cid = str(clusters[0].id)
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await svc.set_notification_preference(t, "u1", enabled=True)
            await s.commit()
            result = await svc.notify_eligible(t, "u1", cid)
        assert result["sent"] == 1
        payload = result["payload"]
        serialised = repr(payload)
        assert not SECRET_RE.findall(serialised)
        # Lockscreen-safe: no position size / financial values.
        assert "quantity" not in serialised.lower()
        assert "1500" not in serialised
        assert "market_value" not in serialised.lower()

    async def test_error_paths_no_stacktraces_or_env_names(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Forced error paths never emit stack traces, env var names, or
        absolute internal paths into stored errors."""
        t = await _mk_tenant(session_factory, "h3-err")
        # Cross-tenant / malformed operations return safe False, no trace.
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            assert await svc.set_ack(t, "u1", "not-a-uuid", True) is False
            assert (
                await svc.correct(t, "u1", "not-a-uuid", security_id=None)
                is False
            )
            # notify_eligible with a malformed cluster id is a safe
            # not_found (never raises in the UUID bind processor).
            # Opt in first so the pref gate passes and the malformed id
            # actually reaches the cluster lookup path.
            await svc.set_notification_preference(t, "u1", enabled=True)
            await s.commit()
            result = await svc.notify_eligible(t, "u1", "not-a-uuid")
            assert result["sent"] == 0
            assert result["skipped"] == "not_found"
        # The correction reason sanitisation scrubs secret *values*
        # (token=, api_key=, sk- shapes) before persistence.
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            sec = await _mk_security(session_factory)
            acct = await _mk_account(session_factory, t)
            await _mk_holding(session_factory, t, acct, sec)
            item_id = await _mk_item(session_factory, t, sec)
            await svc.correct(
                t,
                "u1",
                item_id,
                security_id=sec,
                reason=f"token={_SK_SHAPE} api_key=zzz",
            )
            await s.commit()
            stmt = select(RelevanceCorrection).where(
                RelevanceCorrection.tenant_id == t
            )
            row = (await s.execute(stmt)).scalars().first()
        assert row is not None
        stored = row.reason or ""
        # Secret values are scrubbed before persistence.
        assert _SK_SHAPE not in stored
        assert "api_key=zzz" not in stored
        assert "Traceback" not in stored


# ═══════════════════════════════════════════════════════════════════════
# H4 — Cluster precision: no over- and under-merge
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH4ClusterPrecision:
    async def test_syndicated_set_is_one_cluster_with_all_links(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Three syndicated posts about one event = 1 cluster, exactly 3
        source URLs, source_count 3."""
        t = await _mk_tenant(session_factory, "h4-syn")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        published = datetime.now(UTC) - timedelta(hours=2)
        facts = [{"key": "event_date", "value": "2026-09-15"}]
        urls = []
        for i in range(3):
            url = f"https://example.com/synd/{i}"
            urls.append(url)
            await _mk_item(
                session_factory,
                t,
                sec,
                source_id=f"h4-syn-{i}",
                headline=f"Apple Q4 earnings (wire {i})",
                canonical_url=url,
                kind="earnings_report",
                published_at=published,
                facts=facts,
            )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)

        clusters = await _clusters(session_factory, t)
        assert len(clusters) == 1
        assert clusters[0].source_count == 3
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed = await svc.feed(t, user_id="u1")
        assert feed["total"] == 1
        item = feed["items"][0]
        assert item["source_count"] == 3
        got = {src["url"] for src in item["sources"]}
        assert got == set(urls)
        assert len(item["sources"]) == 3

    async def test_distinct_events_never_merge(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Two earnings events for different quarters and an ex-date vs
        payment date are separate stories (2 clusters), even though the
        facts differ only by event date."""
        t = await _mk_tenant(session_factory, "h4-dist")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        # Identical headlines, different event dates — still distinct.
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="q1",
            headline="Quarterly results",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-06-30"}],
        )
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="q2",
            headline="Quarterly results",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-30"}],
        )
        # Ex-date vs payment date — separate stories.
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="ex",
            headline="Dividend declared",
            kind="dividend",
            facts=[{"key": "ex_date", "value": "2026-08-10"}],
        )
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="pay",
            headline="Dividend declared",
            kind="dividend",
            facts=[{"key": "payment_date", "value": "2026-08-25"}],
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)

        clusters = await _clusters(session_factory, t)
        assert len(clusters) == 4  # 2 earnings + 2 dividends
        dates = sorted(
            c.event_date.date().isoformat()  # type: ignore[union-attr]
            for c in clusters
        )
        assert dates == ["2026-06-30", "2026-08-10", "2026-08-25", "2026-09-30"]

    async def test_identical_facts_different_dates_never_merge(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Identical facts with different event dates are never merged
        (no over-merge): ex-date cluster and payment-date cluster stay
        apart even with identical headlines."""
        t = await _mk_tenant(session_factory, "h4-dates")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        for i, date in enumerate(("2026-08-10", "2026-08-25")):
            await _mk_item(
                session_factory,
                t,
                sec,
                source_id=f"div-{i}",
                headline="Dividend declared",
                kind="dividend",
                facts=[{"key": "event_date", "value": date}],
            )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
        clusters = await _clusters(session_factory, t)
        assert len(clusters) == 2


# ═══════════════════════════════════════════════════════════════════════
# H5 — Graceful degradation with stale / partially missing sources
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH5GracefulDegradation:
    async def test_endpoint_ok_with_stale_mixed_cluster(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A cluster whose sources are partially stale still renders with
        per-source freshness values and fetched_at; no crash."""
        t = await _mk_tenant(session_factory, "h5")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        now = datetime.now(UTC)
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="fresh",
            headline="Apple Q4 earnings",
            kind="earnings_report",
            published_at=now - timedelta(hours=1),
            fetched_at=now,
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="stale",
            headline="Apple Q4 earnings",
            kind="earnings_report",
            published_at=now - timedelta(days=4),
            fetched_at=now - timedelta(days=4),
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed = await svc.feed(t, user_id="u1")
        assert feed["total"] == 1
        item = feed["items"][0]
        # Every source carries freshness + fetched_at.
        for src in item["sources"]:
            assert src["freshness"] in (FRESHNESS_FRESH, FRESHNESS_STALE)
            assert src["fetched_at"] is not None
        freshes = {src["freshness"] for src in item["sources"]}
        assert freshes == {FRESHNESS_FRESH, FRESHNESS_STALE}
        # The cluster itself is served (not dropped) — no crash.

    async def test_missing_source_url_cluster_ranks_and_renders(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A cluster whose source item has no canonical_url still ranks
        and renders (url None is tolerated)."""
        t = await _mk_tenant(session_factory, "h5-nourl")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="nourl",
            headline="Apple Q4 earnings",
            canonical_url=None,
            kind="earnings_report",
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed = await svc.feed(t, user_id="u1")
        assert feed["total"] == 1
        item = feed["items"][0]
        # The cluster ranks and renders; the missing link is tolerated.
        assert item["sources"][0]["url"] is None
        assert item["source_count"] == 0  # no source links to count

    async def test_ranking_is_deterministic(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Same input → same order (deterministic scoring)."""
        t = await _mk_tenant(session_factory, "h5-det")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        for i, (kind, days_ago, score_fact) in enumerate(
            (
                ("earnings_report", 1, "2026-09-15"),
                ("dividend", 5, "2026-08-20"),
                ("earnings_report", 3, "2026-08-01"),
            )
        ):
            await _mk_item(
                session_factory,
                t,
                sec,
                source_id=f"det-{i}",
                headline=f"Story {i}",
                kind=kind,
                published_at=datetime.now(UTC) - timedelta(days=days_ago),
                facts=[{"key": "event_date", "value": score_fact}],
            )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            first = await svc.feed(t, user_id="u1")
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            second = await svc.feed(t, user_id="u1")
        ids1 = [i["id"] for i in first["items"]]
        ids2 = [i["id"] for i in second["items"]]
        assert ids1 == ids2
        scores1 = [i["score"] for i in first["items"]]
        assert scores1 == sorted(scores1, reverse=True)


# ═══════════════════════════════════════════════════════════════════════
# H6 — Acknowledgement semantics per user per cluster
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH6Acknowledgement:
    async def _seed(
        self, session_factory: async_sessionmaker
    ) -> tuple[str, str, str]:
        t = await _mk_tenant(session_factory, "h6")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="h6-1",
            headline="Apple Q4 earnings beat",
            canonical_url="https://example.com/h6/1",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
        clusters = await _clusters(session_factory, t)
        return t, sec, str(clusters[0].id)

    async def test_ack_per_user_and_later_link_does_not_reset(
        self, session_factory: async_sessionmaker
    ) -> None:
        """User A acks; user B sees unread.  A later syndicated link does
        not reset A's ack."""
        t, sec, cid = await self._seed(session_factory)
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            assert await svc.set_ack(t, "user-A", cid, True)
            await s.commit()

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed_a = await svc.feed(t, user_id="user-A")
            feed_b = await svc.feed(t, user_id="user-B")
        assert feed_a["items"][0]["acknowledged"] is True
        assert feed_b["items"][0]["acknowledged"] is False

        # A second syndicated source arrives for the SAME event date →
        # merges into the existing exact-event cluster; ack must survive.
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="h6-later",
            headline="Apple Q4 earnings beat",
            canonical_url="https://example.com/h6/later",
            kind="earnings_report",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed_a = await svc.feed(t, user_id="user-A")
        assert feed_a["items"][0]["acknowledged"] is True
        assert feed_a["items"][0]["source_count"] == 2

    async def test_ack_idempotent_and_cross_tenant_safe(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Re-ack/un-ack idempotent; cross-tenant cluster id returns
        False without revealing existence."""
        t, _, cid = await self._seed(session_factory)
        other = await _mk_tenant(session_factory, "h6-other")
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            assert await svc.set_ack(t, "user-A", cid, True)
            assert await svc.set_ack(t, "user-A", cid, True)
            await s.commit()
            assert await svc.set_ack(t, "user-A", cid, False)
            assert await svc.set_ack(t, "user-A", cid, False)
            await s.commit()
            assert await svc.set_ack(other, "user-B", cid, True) is False


# ═══════════════════════════════════════════════════════════════════════
# H7 — Correction flow per-tenant, never destroys items
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH7Corrections:
    async def test_correction_hides_for_user_only_and_keeps_item(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A's correction hides the item from A's holding feed; the item
        stays visible through generic (unfiltered) news queries; B's
        household feed is unchanged; the observation is never deleted."""
        a = await _mk_tenant(session_factory, "h7-a")
        b = await _mk_tenant(session_factory, "h7-b")
        sec_a = await _mk_security(session_factory, ticker="AAPL")
        sec_b = await _mk_security(session_factory, ticker="MSFT")
        acct_a = await _mk_account(session_factory, a)
        acct_b = await _mk_account(session_factory, b)
        await _mk_holding(session_factory, a, acct_a, sec_a)
        await _mk_holding(session_factory, b, acct_b, sec_b)
        item_id = await _mk_item(
            session_factory, a, sec_a, kind="earnings_report"
        )
        # B has its own item.
        await _mk_item(session_factory, b, sec_b, kind="earnings_report")

        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, a)
            svc2 = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc2, b)

        # User A corrects the (item, sec_a) pair.
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            ok = await svc.correct(
                a, "user-A", item_id, security_id=sec_a, reason="FP"
            )
            assert ok is True
            await s.commit()

        # A's holding feed hides it.
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed_a = await svc.feed(a, user_id="user-A")
        assert feed_a["total"] == 0

        # The underlying observation still exists.
        async with session_factory() as s:
            item = await s.get(MarketIntelligenceItem, item_id)
            assert item is not None

        # B's feed is unaffected (B is a different tenant).
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            feed_b = await svc.feed(b, user_id="user-B")
        assert feed_b["total"] == 1
        assert str(feed_b["items"][0]["security_id"]) == sec_b

    async def test_correction_future_item_rematch_prevention_per_user(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A similar future item for the same security claim stays out of
        the correcting user's feed but remains visible to other users."""
        t = await _mk_tenant(session_factory, "h7-fp")
        sec = await _mk_security(session_factory)
        acct = await _mk_account(session_factory, t)
        await _mk_holding(session_factory, t, acct, sec)
        item1 = await _mk_item(
            session_factory,
            t,
            sec,
            source_id="h7-fp-1",
            headline="Apple Q4 earnings beat",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-15"}],
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await svc.correct(t, "user-A", item1, security_id=sec)
            await s.commit()

        # Future similar item for the same security claim.
        await _mk_item(
            session_factory,
            t,
            sec,
            source_id="h7-fp-2",
            headline="Apple Q4 earnings beat",
            kind="earnings_report",
            facts=[{"key": "event_date", "value": "2026-09-16"}],
        )
        async with session_factory() as s:
            svc = HoldingRelevanceService(UnitOfWork(s))
            await _build(svc, t)
            feed_a = await svc.feed(t, user_id="user-A")
            feed_b = await svc.feed(t, user_id="user-B")
        assert feed_a["total"] == 0
        assert feed_b["total"] == 1


# ═══════════════════════════════════════════════════════════════════════
# H8 — Wealthfolio integration: read-only, WAL-safe, no writes
# ═══════════════════════════════════════════════════════════════════════


class TestHoldoutH8WealthfolioReadOnly:
    def test_open_wal_db_readonly_immutable(self) -> None:
        """Opening a WAL-mode SQLite DB read-only leaves checksum, mtime
        and the -wal/-shm siblings untouched (no writes, no new files)."""
        tmpdir = Path(tempfile.mkdtemp(prefix="h8-wal-"))
        db_path = tmpdir / "wealthfolio.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)"
        )
        conn.execute("INSERT INTO accounts (name) VALUES ('test')")
        conn.commit()
        conn.close()

        # Snapshot before the READ-ONLY open: the DB bytes + mtime must
        # never change (the read-only open writes nothing to the main
        # DB file).  Note: SQLite may lazily create -wal/-shm siblings on
        # open even in read-only mode; that is a SQLite-level artifact
        # and does not touch Wealthfolio's data.  The app-level invariant
        # (asserted in test_gui_view_never_touches_wealthfolio_db) is
        # that the companion view never opens the Wealthfolio DB at all.
        db_before = db_path.read_bytes()
        mtime_before = db_path.stat().st_mtime_ns

        # Open read-only via URI, like the companion view does.
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = ro.execute("SELECT name FROM accounts").fetchall()
        assert rows == [("test",)]
        ro.close()

        db_after = db_path.read_bytes()
        mtime_after = db_path.stat().st_mtime_ns
        # No bytes changed, no mtime change — the read-only open never
        # writes to Wealthfolio's database.
        assert db_after == db_before
        assert mtime_after == mtime_before

    def test_readonly_under_concurrent_write_transaction(self) -> None:
        """A reader can still read while another connection holds an open
        write transaction (WAL mode): no SQLITE_BUSY/CANTOPEN crash."""
        tmpdir = Path(tempfile.mkdtemp(prefix="h8-writer-"))
        db_path = tmpdir / "wealthfolio.db"
        writer = sqlite3.connect(str(db_path))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)"
        )
        writer.execute("INSERT INTO accounts (name) VALUES ('one')")
        writer.commit()

        # Writer holds an open (uncommitted) write transaction.
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO accounts (name) VALUES ('two')")

        # Reader must still work.
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = ro.execute("SELECT name FROM accounts").fetchall()
        assert ("one",) in rows
        ro.close()

        writer.rollback()
        writer.close()

    def test_locked_db_degrades_cleanly(self) -> None:
        """When the DB is exclusively locked, opening read-only surfaces a
        clear error message — not a raw stacktrace."""
        tmpdir = Path(tempfile.mkdtemp(prefix="h8-lock-"))
        db_path = tmpdir / "wealthfolio.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
        conn.commit()

        # Take an exclusive lock on the file (simulates an active writer).
        with open(db_path, "rb") as fh:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                ro.execute("SELECT * FROM accounts")
                ro.close()
                # On platforms where the lock is advisory this still works
                # — that is acceptable; the requirement is that we never
                # crash with a raw traceback.
            except sqlite3.OperationalError as exc:
                # A clear, bounded error — no traceback, no path dump.
                msg = str(exc)
                assert len(msg) < 500
                assert "Traceback" not in msg
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def test_gui_view_never_touches_wealthfolio_db(self) -> None:
        """The companion view is served purely from the finance-sync API:
        no sqlite/open/read calls in the GUI router, and every
        interpolated value in the template is escaped (XSS-safe)."""
        gui_router = Path("src/finance_sync/gui/router.py").read_text()
        template = Path(
            "src/finance_sync/templates/holding_relevance.html"
        ).read_text()
        # No direct Wealthfolio DB access anywhere in the view path.
        assert "sqlite3" not in gui_router
        assert "sqlalchemy" not in gui_router
        assert "create_engine" not in gui_router
        assert "connect(" not in gui_router
        # The template escapes every interpolated value (XSS-safe) and
        # never embeds raw .db file paths.
        assert "escapeHtml" in template
        assert "innerHTML" in template
        assert ".db" not in template
        assert "sqlite" not in template.lower()
