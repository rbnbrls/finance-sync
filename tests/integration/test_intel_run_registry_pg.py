"""Integration tests for the market-intelligence run registry (real PG).

Exercises the scheduler story acceptance criteria against **real**
PostgreSQL (FK constraints, JSONB, UUID pk, timestamptz — none of which
the aiosqlite unit suite can prove):

* every scheduler run is recorded in the append-only run registry with
  started/completed timestamps, duration, quota, freshness snapshot and
  sanitised errors;
* the run registry is tenant-scoped and observable through the read
  service;
* a provider outage never deletes previously valid observations and the
  run trail records the failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import select

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
from finance_sync.models import Tenant
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.market_intelligence_run import (
    MarketIntelligenceRun,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


async def _create_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str | None = None,
) -> Tenant:
    slug = slug or f"t-{uuid4().hex[:12]}"
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.tenants.add(Tenant(slug=slug, name="Intel Run Test"))


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


def _item(
    *,
    provider: str = "sec",
    source_id: str | None = None,
    headline: str = "Test",
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
        license_class=IntelLicenseClass.FREE_ACCESS,
        content_hash=content_hash(
            {"provider": provider, "source_id": sid, "headline": headline}
        ),
        headline=headline,
        summary="Short",
        store_full_text=False,
        store_summary=True,
        identifiers={},
        facts=[],
    )


class _Container:
    """Minimal Container double for the scheduler."""

    def __init__(
        self, sf: async_sessionmaker[AsyncSession], resolver: Any
    ) -> None:
        self.session_factory = sf
        self.security_resolver = resolver


def _ok_provider(key: str) -> IntelProvider:
    class _Ok(IntelProvider):
        provider_key = key

        async def capabilities(self):
            return [IntelCapability.NEWS]

        async def available(self, capability):
            return IntelAvailability.AVAILABLE

        async def fetch(self, capability, *, identifiers=None, limit=20):
            return []

    return _Ok(
        freshness=IntelFreshnessPolicy(
            max_age=timedelta(hours=6), min_interval=timedelta(minutes=15)
        )
    )


# ═══════════════════════════════════════════════════════════════════════
# Run registry on real PG
# ═══════════════════════════════════════════════════════════════════════


class TestRunRegistryPG:
    async def test_run_recorded_with_full_metrics(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A successful run is recorded with timestamps, latency, quota."""
        tenant = await _create_tenant(session_factory)
        resolver = _FakeResolver({})

        class _QuotaProvider(IntelProvider):
            provider_key = "runpg-1"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                return []

            async def quota_usage(self):
                return 7, 500

        scheduler = IntelScheduler(
            _Container(session_factory, resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            str(tenant.id), _QuotaProvider(), capability=None, force=True
        )
        assert result["status"] == "ok"

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MarketIntelligenceRun).where(
                            MarketIntelligenceRun.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(list(rows)) == 1
            run = next(iter(rows))
            assert run.provider == "runpg-1"
            assert run.status == "ok"
            assert run.started_at is not None
            assert run.completed_at is not None
            assert run.completed_at >= run.started_at
            assert run.latency_ms is not None and run.latency_ms >= 0
            assert run.quota_used == 7
            assert run.quota_limit == 500
            assert run.error is None
            assert run.freshness_max_age_seconds == 6 * 3600
            assert run.freshness_min_interval_seconds == 15 * 60
            assert run.capabilities == ["news"]

    async def test_failed_run_keeps_data_and_records_trail(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """An outage never deletes data; the run trail records the failure."""
        tenant = await _create_tenant(session_factory)
        resolver = _FakeResolver({})
        now = datetime.now(UTC)
        item = _item(provider="runpg-2", source_id="keep-1")
        item.fetched_at = now - timedelta(days=7)
        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, resolver)
            await service.ingest_items(str(tenant.id), "runpg-2", [item])
            await uow.commit()

        class _Failing(IntelProvider):
            provider_key = "runpg-2"

            async def capabilities(self):
                return [IntelCapability.NEWS]

            async def available(self, capability):
                return IntelAvailability.AVAILABLE

            async def fetch(self, capability, *, identifiers=None, limit=20):
                from finance_sync.intel.exceptions import (
                    IntelProviderUnavailableError,
                )

                msg = "upstream 503 from api.example.com/key=sk_tes...3456"
                raise IntelProviderUnavailableError(msg)

        scheduler = IntelScheduler(
            _Container(session_factory, resolver), registry=None
        )
        result = await scheduler._refresh_provider(
            str(tenant.id), _Failing(), capability=None, force=True
        )
        assert result["status"] == "unavailable"

        async with session_factory() as session:
            # The observation survives (soft staleness only).
            items = (
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
            assert len(list(items)) == 1
            row = next(iter(items))
            assert row.headline is not None
            assert row.is_stale is True  # soft flag, still present

            # The run trail records the sanitised failure.
            runs = (
                (
                    await session.execute(
                        select(MarketIntelligenceRun).where(
                            MarketIntelligenceRun.tenant_id == str(tenant.id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(list(runs)) == 1
            run = next(iter(runs))
            assert run.status == "unavailable"
            assert run.error is not None
            assert "503" in run.error
            assert "sk_test_abcdef123456" not in run.error  # redacted
            assert run.completed_at is not None

    async def test_run_registry_is_tenant_scoped(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Runs of tenant A are invisible to tenant B."""
        tenant_a = await _create_tenant(session_factory, slug=f"a-{uuid4().hex[:8]}")
        tenant_b = await _create_tenant(session_factory, slug=f"b-{uuid4().hex[:8]}")
        resolver = _FakeResolver({})

        scheduler = IntelScheduler(
            _Container(session_factory, resolver), registry=None
        )
        for tenant in (tenant_a, tenant_b):
            await scheduler._refresh_provider(
                str(tenant.id),
                _ok_provider("runpg-3"),
                capability=None,
                force=True,
            )

        from finance_sync.services.market_intelligence_read import (
            MarketIntelligenceReadService,
        )

        async with session_factory() as session:
            read = MarketIntelligenceReadService(session)
            runs_a = await read.list_runs(str(tenant_a.id))
            assert len(runs_a) == 1
            assert runs_a[0].provider == "runpg-3"
            # Tenant B sees only its own run.
            runs_b = await read.list_runs(str(tenant_b.id))
            assert len(runs_b) == 1
            # Cross-tenant: B's run is not in A's list.
            assert all(r.provider == "runpg-3" for r in runs_a)
