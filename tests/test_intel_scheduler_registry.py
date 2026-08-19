"""Tests for the market-intelligence scheduler, run registry and isolation.

Covers the acceptance criteria of the scheduler story:

* **Scheduling cadence** — each provider is refreshed on its own
  freshness policy (max_age / min_interval), never inside a rate-limit
  window, and per-provider settings override the adapter defaults.
* **Run registration** — every run (ok / unavailable / degraded) is
  recorded in the append-only run registry with started/completed
  timestamps, duration, quota, freshness snapshot and sanitised errors;
  the latest-run state row stays consistent.
* **Stale detection** — failed runs soft-flag old items stale (never
  deleted); successful runs clear the flag.
* **Failure isolation** — a provider that crashes (even in capability
  discovery) never blocks sibling providers nor the rest of the tick;
  a slow provider times out within its bounded window.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


def _uuid_bind_patched(self, dialect):
    proc = _uuid_bind_orig(self, dialect)
    if proc is None or not self.as_uuid:
        return proc

    def _patched(value):
        if value is not None:
            if isinstance(value, str):
                return _uuid_mod.UUID(value).hex
            return value.hex
        return value

    return _patched


_sa_types.Uuid.bind_processor = _uuid_bind_patched

from finance_sync.db import Base
from finance_sync.db.uow import UnitOfWork
from finance_sync.intel.enums import (
    IntelAvailability,
    IntelCapability,
    IntelItemKind,
    IntelLicenseClass,
)
from finance_sync.intel.hashing import content_hash
from finance_sync.intel.models import IntelItem
from finance_sync.intel.provider import (
    IntelFreshnessPolicy,
    IntelProvider,
)
from finance_sync.intel.scheduler import IntelScheduler
from finance_sync.intel.service import IntelIngestionService
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.market_intelligence_provider_state import (
    MarketIntelligenceProviderState,
)
from finance_sync.models.market_intelligence_run import (
    MarketIntelligenceRun,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def engine() -> AsyncEngine:
    return create_async_engine("sqlite+aiosqlite://", echo=False)


@pytest.fixture
async def tables(engine: AsyncEngine) -> Any:
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


class _FakeResolver:
    """SecurityResolver double with no matches."""

    async def resolve_by_isin(self, isin: str) -> Any:
        return None

    async def resolve_by_figi(self, figi: str) -> Any:
        return None

    async def resolve_by_ticker(self, ticker: str) -> Any:
        return None


@pytest.fixture
def fake_resolver() -> _FakeResolver:
    return _FakeResolver()


def _item(
    *,
    provider: str = "sec",
    source_id: str | None = None,
    headline: str = "Test headline",
    published: datetime | None = None,
) -> IntelItem:
    now = datetime.now(UTC)
    sid = source_id or str(uuid4())
    return IntelItem(
        provider=provider,
        source_id=sid,
        canonical_url=f"https://example.com/{sid}",
        kind=IntelItemKind.NEWS_ARTICLE,
        published_at=published or now,
        fetched_at=now,
        language="en",
        license_class=IntelLicenseClass.FREE_ACCESS,
        content_hash=content_hash(
            {"provider": provider, "source_id": sid, "headline": headline}
        ),
        headline=headline,
        summary="Short summary",
        store_full_text=False,
        store_summary=True,
        identifiers={},
        facts=[],
    )


class _Container:
    """Minimal Container double for the scheduler."""

    def __init__(
        self,
        sf: async_sessionmaker,
        resolver: Any,
        settings: Any | None = None,
    ) -> None:
        self.session_factory = sf
        self.security_resolver = resolver
        self.settings = settings


async def _count_runs(
    session_factory: async_sessionmaker,
    tenant_id: str,
) -> list[MarketIntelligenceRun]:
    async with session_factory() as session:
        stmt = (
            select(MarketIntelligenceRun)
            .where(MarketIntelligenceRun.tenant_id == tenant_id)
            .order_by(MarketIntelligenceRun.started_at)
        )
        return list((await session.execute(stmt)).scalars().all())


# ═══════════════════════════════════════════════════════════════════════
# Scheduling cadence
# ═══════════════════════════════════════════════════════════════════════


class TestSchedulingCadence:
    async def test_is_due_when_never_run(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A provider that never ran is due immediately."""
        provider = _make_ok_provider("cad-1")
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        assert await scheduler._is_due(str(uuid4()), provider) is True

    async def test_not_due_within_min_interval(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A recent run defers the next one (min_interval respected)."""
        tenant_id = str(uuid4())
        provider = _make_ok_provider(
            "cad-2",
            freshness=IntelFreshnessPolicy(
                max_age=timedelta(hours=6), min_interval=timedelta(hours=1)
            ),
        )
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        # Simulate a run 5 minutes ago.
        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, fake_resolver)
            await service.record_provider_run(
                tenant_id,
                provider,
                capability=IntelCapability.NEWS,
                status="ok",
                latency_ms=10,
                started_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            await uow.commit()
        assert await scheduler._is_due(tenant_id, provider) is False

    async def test_due_after_max_age(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """Data older than max_age is due for a refresh."""
        tenant_id = str(uuid4())
        provider = _make_ok_provider(
            "cad-3",
            freshness=IntelFreshnessPolicy(
                max_age=timedelta(hours=6), min_interval=timedelta(minutes=15)
            ),
        )
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, fake_resolver)
            await service.record_provider_run(
                tenant_id,
                provider,
                capability=IntelCapability.NEWS,
                status="ok",
                latency_ms=10,
                started_at=datetime.now(UTC) - timedelta(hours=7),
            )
            await uow.commit()
        assert await scheduler._is_due(tenant_id, provider) is True

    async def test_unavailable_backs_off_to_max_age(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A failing provider is retried only after its max_age (backoff)."""
        tenant_id = str(uuid4())
        provider = _make_ok_provider(
            "cad-4",
            freshness=IntelFreshnessPolicy(
                max_age=timedelta(hours=6), min_interval=timedelta(minutes=15)
            ),
        )
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, fake_resolver)
            await service.record_provider_run(
                tenant_id,
                provider,
                capability=IntelCapability.NEWS,
                status="unavailable",
                error="upstream 503",
                started_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            await uow.commit()
        # Not due yet — inside the backoff window.
        assert await scheduler._is_due(tenant_id, provider) is False
        # After max_age it is due again.
        async with session_factory() as session:
            uow = UnitOfWork(session)
            state = (
                await session.execute(
                    select(MarketIntelligenceProviderState).where(
                        MarketIntelligenceProviderState.tenant_id == tenant_id
                    )
                )
            ).scalars().first()
            state.last_run_at = datetime.now(UTC) - timedelta(hours=7)
            await uow.commit()
        assert await scheduler._is_due(tenant_id, provider) is True

    async def test_refresh_all_skips_not_due(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """refresh_all skips providers that are not due."""
        tenant_id = str(uuid4())
        provider = _make_ok_provider("cad-5")
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        # Force a run now (not due path).
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        assert result["status"] == "ok"
        # Now not due.
        result2 = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=False
        )
        assert result2["status"] == "skipped"
        # Only one run recorded.
        runs = await _count_runs(session_factory, tenant_id)
        assert len(runs) == 1
        assert runs[0].status == "ok"


# ═══════════════════════════════════════════════════════════════════════
# Run registration
# ═══════════════════════════════════════════════════════════════════════


class TestRunRegistration:
    async def test_successful_run_records_history(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A successful run is recorded in state + run registry."""
        tenant_id = str(uuid4())
        provider = _make_ok_provider("run-1")
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        assert result["status"] == "ok"

        runs = await _count_runs(session_factory, tenant_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "ok"
        assert run.provider == "run-1"
        assert run.started_at is not None
        assert run.completed_at is not None
        assert run.completed_at >= run.started_at
        assert run.latency_ms is not None and run.latency_ms >= 0
        assert run.error is None
        assert run.error_class is None
        # Freshness snapshot recorded.
        assert run.freshness_max_age_seconds == int(
            provider.freshness.max_age.total_seconds()
        )
        assert run.freshness_min_interval_seconds == int(
            provider.freshness.min_interval.total_seconds()
        )

        # Latest-run state is consistent.
        async with session_factory() as session:
            state = (
                await session.execute(
                    select(MarketIntelligenceProviderState).where(
                        MarketIntelligenceProviderState.tenant_id == tenant_id,
                        MarketIntelligenceProviderState.provider == "run-1",
                    )
                )
            ).scalars().first()
            assert state.status == "ok"
            assert state.last_success_at is not None
            assert state.last_error is None

    async def test_failed_run_records_sanitised_error(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A failed run records an unavailable status + sanitised error."""
        tenant_id = str(uuid4())

        class _Failing(IntelProvider):
            provider_key = "run-2"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                from finance_sync.intel.exceptions import (
                    IntelProviderUnavailableError,
                )

                msg = "upstream 503 from api.example.com/secret=abc123def456"
                raise IntelProviderUnavailableError(msg)

        provider = _Failing()
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        # A single capability that fails inside the bounded loop means
        # the whole run failed (all capabilities failed) — never "ok".
        assert result["status"] == "unavailable"

        runs = await _count_runs(session_factory, tenant_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "unavailable"
        assert run.completed_at is not None
        assert run.latency_ms is not None
        # Error recorded and sanitised (secret-shaped token redacted).
        assert run.error is not None
        assert "503" in run.error
        assert "abc123def456" not in run.error
        assert run.error_class is not None

    async def test_quota_recorded_on_success(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """Quota usage is captured and recorded when the provider reports it."""
        tenant_id = str(uuid4())

        class _QuotaProvider(IntelProvider):
            provider_key = "run-3"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                return []

            async def quota_usage(self):
                return 42, 1000

        provider = _QuotaProvider()
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        assert result["status"] == "ok"

        runs = await _count_runs(session_factory, tenant_id)
        assert runs[0].quota_used == 42
        assert runs[0].quota_limit == 1000

        async with session_factory() as session:
            state = (
                await session.execute(
                    select(MarketIntelligenceProviderState).where(
                        MarketIntelligenceProviderState.tenant_id == tenant_id
                    )
                )
            ).scalars().first()
            assert state.quota_used == 42
            assert state.quota_limit == 1000

    async def test_quota_probe_failure_does_not_fail_run(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A quota probe that raises never fails the run."""
        tenant_id = str(uuid4())

        class _QuotaBroken(IntelProvider):
            provider_key = "run-4"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                return []

            async def quota_usage(self):
                msg = "quota endpoint down"
                raise RuntimeError(msg)

        provider = _QuotaBroken()
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        assert result["status"] == "ok"

        runs = await _count_runs(session_factory, tenant_id)
        assert runs[0].status == "ok"
        assert runs[0].quota_used is None
        assert runs[0].quota_limit is None

    async def test_items_ingested_recorded(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A run that ingests items records them in the run registry."""
        tenant_id = str(uuid4())
        item = _item(provider="run-5", source_id="i1")

        class _Provider(IntelProvider):
            provider_key = "run-5"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                return [item]

        provider = _Provider()
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        assert result["status"] == "ok"

        async with session_factory() as session:
            items = (
                await session.execute(
                    select(MarketIntelligenceItem).where(
                        MarketIntelligenceItem.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            assert len(list(items)) == 1


# ═══════════════════════════════════════════════════════════════════════
# Stale detection
# ═══════════════════════════════════════════════════════════════════════


class TestStaleDetection:
    async def test_failed_run_marks_old_items_stale(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A failing run soft-flags old items, keeps them queryable."""
        tenant_id = str(uuid4())
        now = datetime.now(UTC)
        old = _item(provider="stale-1", source_id="old-1", published=now)
        old.fetched_at = now - timedelta(days=7)

        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, fake_resolver)
            await service.ingest_items(tenant_id, "stale-1", [old])
            await uow.commit()

        class _Failing(IntelProvider):
            provider_key = "stale-1"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                from finance_sync.intel.exceptions import (
                    IntelProviderUnavailableError,
                )

                msg = "upstream 503"
                raise IntelProviderUnavailableError(msg)

        provider = _Failing(
            freshness=IntelFreshnessPolicy(
                max_age=timedelta(hours=6), min_interval=timedelta(minutes=15)
            )
        )
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        assert result["status"] == "unavailable"

        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(MarketIntelligenceItem).where(
                        MarketIntelligenceItem.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            assert len(list(rows)) == 1
            row = next(iter(rows))
            assert row.is_stale is True  # soft flag, still present
            assert row.headline is not None

    async def test_successful_run_clears_stale(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A successful run clears the stale flag (recovery observable)."""
        tenant_id = str(uuid4())
        now = datetime.now(UTC)
        item = _item(provider="stale-2", source_id="rec-1", published=now)
        item.fetched_at = now - timedelta(days=7)

        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, fake_resolver)
            await service.ingest_items(tenant_id, "stale-2", [item])
            await service.mark_stale(
                tenant_id, "stale-2", older_than=now - timedelta(hours=1)
            )
            await uow.commit()

        provider = _make_ok_provider(
            "stale-2",
            freshness=IntelFreshnessPolicy(
                max_age=timedelta(hours=6), min_interval=timedelta(minutes=15)
            ),
        )
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        assert result["status"] == "ok"

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(MarketIntelligenceItem).where(
                        MarketIntelligenceItem.tenant_id == tenant_id
                    )
                )
            ).scalars().first()
            assert row.is_stale is False
            assert row.stale_after is None


# ═══════════════════════════════════════════════════════════════════════
# Failure isolation
# ═══════════════════════════════════════════════════════════════════════


class TestFailureIsolation:
    async def test_crashed_provider_does_not_block_siblings(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A provider that crashes in capabilities() never blocks siblings."""
        tenant_id = str(uuid4())

        class _CrashCapabilities(IntelProvider):
            provider_key = "crash-1"

            async def capabilities(self):
                msg = "boom"
                raise RuntimeError(msg)

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                return []

        class _Healthy(IntelProvider):
            provider_key = "healthy-1"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                return []

        from finance_sync.intel.registry import IntelProviderRegistry

        registry = IntelProviderRegistry(
            providers=[_CrashCapabilities(), _Healthy()]
        )
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=registry
        )
        summary = await scheduler.refresh_all(tenant_id, force=True)

        # The crashing provider is recorded unavailable...
        assert summary["providers"]["crash-1"]["status"] == "unavailable"
        # ...but the healthy sibling still ran.
        assert summary["providers"]["healthy-1"]["status"] == "ok"

        runs = await _count_runs(session_factory, tenant_id)
        assert len(runs) == 2
        by_provider = {r.provider: r for r in runs}
        assert by_provider["crash-1"].status == "unavailable"
        assert by_provider["healthy-1"].status == "ok"

    async def test_slow_provider_times_out_isolated(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """A slow provider times out within its bounded window."""
        tenant_id = str(uuid4())

        class _Slow(IntelProvider):
            provider_key = "slow-1"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                await asyncio.sleep(5)
                return []

        provider = _Slow()
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver),
            registry=None,
            run_timeout=0.5,
        )
        started = asyncio.get_event_loop().time()
        result = await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        elapsed = asyncio.get_event_loop().time() - started
        assert elapsed < 3.0
        assert result["status"] == "unavailable"

        runs = await _count_runs(session_factory, tenant_id)
        assert runs[0].status == "unavailable"

    async def test_refresh_all_never_raises(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """refresh_all always returns a summary, never raises."""
        tenant_id = str(uuid4())

        class _Crash(IntelProvider):
            provider_key = "boom-1"

            async def capabilities(self):
                msg = "kaput"
                raise RuntimeError(msg)

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                return []

        from finance_sync.intel.registry import IntelProviderRegistry

        registry = IntelProviderRegistry(providers=[_Crash()])
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=registry
        )
        summary = await scheduler.refresh_all(tenant_id, force=True)
        assert summary["providers"]["boom-1"]["status"] == "unavailable"
        assert "providers" in summary


# ═══════════════════════════════════════════════════════════════════════
# Run observability (read service)
# ═══════════════════════════════════════════════════════════════════════


class TestRunObservability:
    async def test_list_runs_returns_history_newest_first(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """The read service exposes the run history, newest first."""
        tenant_id = str(uuid4())
        provider = _make_ok_provider("obs-1")
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        # Two forced runs → two registry rows.
        await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )
        await scheduler._refresh_provider(
            tenant_id, provider, capability=None, force=True
        )

        from finance_sync.services.market_intelligence_read import (
            MarketIntelligenceReadService,
        )

        async with session_factory() as session:
            read = MarketIntelligenceReadService(session)
            runs = await read.list_runs(tenant_id)
            assert len(runs) == 2
            assert runs[0].started_at >= runs[1].started_at
            assert runs[0].provider == "obs-1"
            assert runs[0].status == "ok"

            # Provider filter works.
            filtered = await read.list_runs(tenant_id, provider="nope")
            assert filtered == []

            # Status filter works.
            failed = await read.list_runs(tenant_id, status="unavailable")
            assert failed == []

    async def test_list_runs_is_tenant_scoped(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """Tenant B cannot see tenant A's runs."""
        tenant_a = str(uuid4())
        tenant_b = str(uuid4())
        provider = _make_ok_provider("obs-2")
        scheduler = IntelScheduler(
            _Container(session_factory, fake_resolver), registry=None
        )
        await scheduler._refresh_provider(
            tenant_a, provider, capability=None, force=True
        )

        from finance_sync.services.market_intelligence_read import (
            MarketIntelligenceReadService,
        )

        async with session_factory() as session:
            read = MarketIntelligenceReadService(session)
            assert len(await read.list_runs(tenant_a)) == 1
            assert await read.list_runs(tenant_b) == []


# ═══════════════════════════════════════════════════════════════════════
# Per-provider configurable cadence (settings overrides)
# ═══════════════════════════════════════════════════════════════════════


class TestConfigurableCadence:
    async def test_registry_applies_settings_overrides(self) -> None:
        """The settings-driven registry honours per-provider overrides."""
        from finance_sync.config.settings import Settings
        from finance_sync.intel.registry import build_intel_registry

        settings = Settings(  # type: ignore[call-arg]
            database_url=None,
            openbb_api_key=None,
            intel_sec_freshness_max_age_seconds=7200,
            intel_sec_freshness_min_interval_seconds=600,
            intel_sec_press_freshness_max_age_seconds=14400,
            intel_openbb_freshness_min_interval_seconds=300,
        )
        registry = build_intel_registry(settings)
        sec = registry.get("sec")
        assert sec is not None
        assert sec.freshness.max_age.total_seconds() == 7200
        assert sec.freshness.min_interval.total_seconds() == 600

        sec_press = registry.get("sec_press")
        assert sec_press is not None
        assert sec_press.freshness.max_age.total_seconds() == 14400
        # Unset min_interval falls back to the adapter default (900 s).
        assert sec_press.freshness.min_interval.total_seconds() == 900

        openbb = registry.get("openbb")
        assert openbb is not None
        assert openbb.freshness.min_interval.total_seconds() == 300
        # Unset max_age falls back to the adapter default (21600 s).
        assert openbb.freshness.max_age.total_seconds() == 21600

    async def test_registry_defaults_when_no_overrides(self) -> None:
        """Without overrides the adapter defaults apply."""
        from finance_sync.config.settings import Settings
        from finance_sync.intel.registry import build_intel_registry

        settings = Settings(  # type: ignore[call-arg]
            database_url=None,
            openbb_api_key=None,
        )
        registry = build_intel_registry(settings)
        sec = registry.get("sec")
        assert sec is not None
        assert sec.freshness.max_age.total_seconds() == 24 * 3600
        assert sec.freshness.min_interval.total_seconds() == 3600


# ── Helpers ───────────────────────────────────────────────────────────


def _make_ok_provider(
    key: str,
    *,
    freshness: IntelFreshnessPolicy | None = None,
) -> IntelProvider:
    """Return a provider whose refresh succeeds with no items."""

    class _Ok(IntelProvider):
        provider_key = key

        async def capabilities(self):
            return [IntelCapability.NEWS]

        async def available(self, capability):
            return IntelAvailability.AVAILABLE

        async def fetch(self, capability, *, identifiers=None, limit=20):
            return []

    return _Ok(freshness=freshness)
