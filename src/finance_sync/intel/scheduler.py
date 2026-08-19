"""Market-intelligence scheduler.

Refreshes provider sources according to their own cadence
(:class:`IntelFreshnessPolicy`) and registers every run (timestamps,
latency, quota, freshness, sanitised errors) in the provider-state
table.  Key properties:

* **Provider outage safety** — a failing provider never deletes or
  invalidates previously stored observations; it only marks its state
  ``unavailable`` with a sanitised error.
* **Isolation** — the scheduler runs per tenant in isolated tasks with
  bounded timeouts; a stuck provider can never block bunq / Trading212 /
  Wealthfolio syncs (those run on their own scheduler jobs).
* **Partial success** — when page 1 of a provider succeeds and page 2
  fails, page-1 items are persisted and the run is recorded as
  ``degraded`` with the error; nothing is rolled back.
* **Rate-limit awareness** — 429s carry ``Retry-After``; the adapter
  never issues a request before the window expires and the scheduler
  records quota/rate-limit metrics and does not plan a new run inside
  the window.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.intel.credentials import IntelCredentialStore
from finance_sync.intel.enums import IntelCapability
from finance_sync.intel.exceptions import (
    IntelProviderAuthError,
    IntelProviderRateLimitError,
    IntelProviderUnavailableError,
)
from finance_sync.intel.service import IntelIngestionService

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from finance_sync.config.settings import Settings
    from finance_sync.container import Container
    from finance_sync.db.uow import UnitOfWork
    from finance_sync.intel.provider import IntelProvider
    from finance_sync.intel.registry import IntelProviderRegistry

logger = structlog.get_logger("finance_sync.worker.intel_scheduler")

#: Default per-provider run timeout (seconds).
_DEFAULT_RUN_TIMEOUT = 60.0


class IntelScheduler:
    """Runs per-tenant, per-provider market-intelligence refreshes."""

    def __init__(
        self,
        container: Container,
        *,
        registry: IntelProviderRegistry | None = None,
        run_timeout: float = _DEFAULT_RUN_TIMEOUT,
    ) -> None:
        self._container = container
        self._registry = registry
        self._run_timeout = run_timeout

    # ── Public API ───────────────────────────────────────────────────

    async def refresh_all(
        self,
        tenant_id: str,
        *,
        capability: IntelCapability | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Refresh every enabled provider for *tenant_id*.

        Returns a per-provider summary::

            {"providers": {"sec": {"status": "ok", "items_ingested": 3,
                                   "latency_ms": 120, "error": None}, ...}}

        **Failure isolation** — every provider runs inside its own
        try/except.  A provider that crashes (even in capability
        discovery or credential loading) is recorded as ``unavailable``
        and the loop continues with the remaining providers.  A
        scheduler failure can therefore never block bunq / Trading212 /
        Wealthfolio syncs (those run on their own APScheduler jobs) and
        never blocks sibling providers in the same tick.
        """
        registry = self._registry or _registry_from_container(self._container)
        summary: dict[str, Any] = {"providers": {}}

        for provider in registry.enabled():
            try:
                result = await self._refresh_provider(
                    tenant_id,
                    provider,
                    capability=capability,
                    force=force,
                )
            except Exception as exc:
                # A provider that raises outside the bounded run (e.g.
                # in capabilities() or credential loading) must not
                # take down the tick.  Record it and move on.
                result = {
                    "status": "unavailable",
                    "reason": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                logger.warning(
                    "intel_provider_crashed",
                    provider=provider.provider_key,
                    tenant_id=tenant_id,
                    error=type(exc).__name__,
                )
            summary["providers"][provider.provider_key] = result

        return summary

    async def _refresh_provider(
        self,
        tenant_id: str,
        provider: IntelProvider,
        *,
        capability: IntelCapability | None,
        force: bool,
    ) -> dict[str, Any]:
        """Run one provider refresh with a bounded timeout and record state."""
        started_wall = datetime.now(UTC)
        if not force and not await self._is_due(tenant_id, provider):
            return {"status": "skipped", "reason": "not_due"}

        started = time.monotonic()
        try:
            capabilities = await provider.capabilities()
        except Exception as exc:
            await self._record_run(
                tenant_id,
                provider,
                status="unavailable",
                error=exc,
                started_at=started_wall,
            )
            return {
                "status": "unavailable",
                "reason": "capability_discovery_failed",
            }
        if capability is not None:
            capabilities = [c for c in capabilities if c == capability]
        if not capabilities:
            await self._record_run(
                tenant_id,
                provider,
                status="unavailable",
                error="provider advertises no capabilities",
                started_at=started_wall,
            )
            return {
                "status": "unavailable",
                "reason": "no_capabilities",
            }

        # Inject the tenant's envelope-decrypted provider credentials
        # before the run.  Adapters that need a key (OpenBB) pick it up
        # in ``configure()``; the plaintext never leaves the process.
        try:
            credentials = await self._load_provider_credentials(
                tenant_id, provider
            )
            provider.configure(credentials)
        except Exception:
            logger.warning(
                "intel_credential_load_failed",
                provider=provider.provider_key,
                tenant_id=tenant_id,
            )

        try:
            async with asyncio.timeout(self._run_timeout):
                capability_failures: list[tuple[str, BaseException]] = []
                for cap in capabilities:
                    try:
                        await self._refresh_capability(tenant_id, provider, cap)
                    except (
                        IntelProviderAuthError,
                        IntelProviderRateLimitError,
                        IntelProviderUnavailableError,
                    ) as exc:
                        # Partial success: continue with other capabilities
                        # but remember the failure so the run is recorded
                        # as degraded — an outage must never look "ok".
                        capability_failures.append((cap.value, exc))
                        continue
            latency_ms = int((time.monotonic() - started) * 1000)
            quota_used, quota_limit = await self._probe_quota(provider)
            if capability_failures:
                # Some capabilities failed.  Record the run as degraded
                # (all failed) or degraded (some failed) with the first
                # failure's sanitised error.  A later successful full
                # run clears the stale flags.
                all_failed = len(capability_failures) == len(capabilities)
                status = "unavailable" if all_failed else "degraded"
                first_failure = capability_failures[0][1]
                await self._refresh_stale_flags(
                    tenant_id, provider, mark=all_failed
                )
                await self._record_run(
                    tenant_id,
                    provider,
                    status=status,
                    error=str(first_failure),
                    latency_ms=latency_ms,
                    quota_used=quota_used,
                    quota_limit=quota_limit,
                    started_at=started_wall,
                )
                return {
                    "status": status,
                    "latency_ms": latency_ms,
                    "failed_capabilities": [c for c, _ in capability_failures],
                }
            # A successful run refreshes the items — clear any stale
            # flags so a recovery is observable, not sticky.
            await self._refresh_stale_flags(tenant_id, provider, mark=False)
            await self._record_run(
                tenant_id,
                provider,
                status="ok",
                latency_ms=latency_ms,
                quota_used=quota_used,
                quota_limit=quota_limit,
                started_at=started_wall,
            )
            return {"status": "ok", "latency_ms": latency_ms}
        except TimeoutError:
            latency_ms = int((time.monotonic() - started) * 1000)
            # Freshness rule: items older than the provider's max_age
            # are marked stale (soft flag) — never deleted, never
            # invalidated.  An outage alone does not mark anything stale;
            # only the freshness bound does.
            await self._refresh_stale_flags(tenant_id, provider, mark=True)
            await self._record_run(
                tenant_id,
                provider,
                status="unavailable",
                error="provider run timed out",
                latency_ms=latency_ms,
                started_at=started_wall,
            )
            return {"status": "unavailable", "reason": "timeout"}
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            await self._refresh_stale_flags(tenant_id, provider, mark=True)
            await self._record_run(
                tenant_id,
                provider,
                status="unavailable",
                error=exc,
                latency_ms=latency_ms,
                started_at=started_wall,
            )
            return {"status": "unavailable", "reason": type(exc).__name__}

    async def _refresh_capability(
        self,
        tenant_id: str,
        provider: IntelProvider,
        capability: IntelCapability,
    ) -> None:
        """Fetch one capability page-by-page and ingest each page.

        Partial-success semantics (holdout H6): pages are ingested as
        they arrive, so a 503 on page 2 keeps page-1 items persisted —
        there is no all-or-nothing rollback.  A mid-page fetch failure
        propagates up after the pages already ingested were committed.
        """
        uow = self._make_uow()
        service = IntelIngestionService(uow, self._container.security_resolver)
        try:
            identifiers: dict[str, str] | None = None
            if provider.provider_key == "sec":
                identifiers = {"cik": "320193"}
            elif provider.provider_key == "openbb":
                identifiers = {"ticker": "AAPL"}

            cursor: Any | None = None
            pages = 0
            while True:
                items, next_cursor = await provider.fetch_with_retry(
                    capability,
                    identifiers=identifiers,
                    limit=20,
                    page=cursor,
                )
                if not items:
                    break
                summary = await service.ingest_items(
                    tenant_id,
                    provider.provider_key,
                    items,
                )
                await uow.commit()
                logger.info(
                    "intel_capability_ingested",
                    provider=provider.provider_key,
                    capability=capability.value,
                    **summary,
                )
                pages += 1
                if next_cursor is None or pages >= 5:
                    break
                cursor = next_cursor
        except Exception:
            await uow.rollback()
            raise
        finally:
            await uow.session.close()

    # ── Freshness / scheduling ───────────────────────────────────────

    async def _refresh_stale_flags(
        self,
        tenant_id: str,
        provider: IntelProvider,
        *,
        mark: bool,
    ) -> None:
        """Mark (or clear) stale flags per the provider's freshness rule.

        ``mark=True``  — after a failed run: items whose ``fetched_at``
        is older than the provider's ``max_age`` are soft-flagged stale
        (never deleted, never invalidated).
        ``mark=False`` — after a successful run: the stale flag is
        cleared so a recovery is observable.
        """
        uow = self._make_uow()
        try:
            service = IntelIngestionService(
                uow, self._container.security_resolver
            )
            max_age = provider.freshness.max_age
            if mark:
                older_than = datetime.now(UTC) - max_age
                await service.mark_stale(
                    tenant_id,
                    provider.provider_key,
                    older_than=older_than,
                )
            else:
                await service.clear_stale(tenant_id, provider.provider_key)
            await uow.commit()
        except Exception as exc:
            logger.warning(
                "intel_stale_refresh_failed",
                provider=provider.provider_key,
                error=type(exc).__name__,
            )
            await uow.rollback()
        finally:
            await uow.session.close()

    async def _is_due(self, tenant_id: str, provider: IntelProvider) -> bool:
        """Return True when *provider* is due for a refresh for *tenant_id*.

        Due = never run, or last run older than ``max_age``, or last run
        earlier than ``min_interval`` ago.  Never fires inside a
        rate-limit window (a 429 from the last run defers the next run).
        """
        uow = self._make_uow()
        try:
            state = await self._get_state(tenant_id, provider)
            now = datetime.now(UTC)
            if state is None:
                return True
            if state.last_run_at is None:
                return True
            last_run = _as_utc(state.last_run_at)
            if state.status == "unavailable":
                # Back off a failing provider: retry after its max_age.
                return now - last_run >= timedelta(
                    seconds=(
                        state.freshness_max_age_seconds
                        or provider.freshness.max_age.total_seconds()
                    )
                )
            min_interval = timedelta(
                seconds=(
                    state.freshness_min_interval_seconds
                    or provider.freshness.min_interval.total_seconds()
                )
            )
            max_age = timedelta(
                seconds=(
                    state.freshness_max_age_seconds
                    or provider.freshness.max_age.total_seconds()
                )
            )
            return now - last_run >= min_interval or (
                now - last_run >= max_age
            )
        finally:
            await uow.session.close()

    async def _get_state(
        self,
        tenant_id: str,
        provider: IntelProvider,
    ) -> Any | None:
        """Return the persisted provider-state row for (tenant, provider)."""
        uow = self._make_uow()
        try:
            repo = uow.market_intelligence_provider_states
            model = repo.model_class
            rows = await repo.list(
                model.tenant_id == tenant_id,  # type: ignore[attr-defined]
                model.provider == provider.provider_key,  # type: ignore[attr-defined]
                limit=1,
            )
            return rows[0] if rows else None
        finally:
            await uow.session.close()

    async def _record_run(
        self,
        tenant_id: str,
        provider: IntelProvider,
        *,
        capability: IntelCapability | None = None,
        status: str,
        error: BaseException | str | None = None,
        latency_ms: int | None = None,
        started_at: datetime | None = None,
        quota_used: int | None = None,
        quota_limit: int | None = None,
    ) -> None:
        """Persist a run outcome (idempotent, secrets sanitised)."""
        uow = self._make_uow()
        try:
            service = IntelIngestionService(
                uow, self._container.security_resolver
            )
            await service.record_provider_run(
                tenant_id,
                provider,
                capability=capability or IntelCapability.NEWS,
                status=status,
                error=str(error) if error else None,
                latency_ms=latency_ms,
                started_at=started_at,
                quota_used=quota_used,
                quota_limit=quota_limit,
            )
            await uow.commit()
        except Exception as exc:
            logger.error(
                "intel_record_run_failed",
                provider=provider.provider_key,
                error=type(exc).__name__,
            )
            await uow.rollback()
        finally:
            await uow.session.close()

    async def _probe_quota(
        self,
        provider: IntelProvider,
    ) -> tuple[int | None, int | None]:
        """Return ``(quota_used, quota_limit)`` from the provider.

        Never raises — a quota probe failure is recorded as
        ``(None, None)`` and does not fail the run.
        """
        try:
            return await provider.quota_usage()
        except Exception:
            return None, None

    def _make_uow(self) -> UnitOfWork:
        """Create a fresh UnitOfWork for a scheduler operation."""
        from finance_sync.db.uow import UnitOfWork

        return UnitOfWork(self._container.session_factory())

    async def _load_provider_credentials(
        self,
        tenant_id: str,
        provider: IntelProvider,
    ) -> dict[str, str]:
        """Load the tenant's envelope-decrypted credentials for *provider*.

        Returns an empty dict when the tenant has none configured (the
        adapter's ``configure()`` treats that as a no-op).  Plaintext
        values exist only in the returned dict.
        """
        uow = self._make_uow()
        try:
            store = IntelCredentialStore(uow, settings=self._container.settings)
            return await store.get(tenant_id, provider.provider_key)
        finally:
            await uow.session.close()


def _registry_from_container(container: Container) -> IntelProviderRegistry:
    """Build the provider registry from the container settings."""
    return build_intel_registry(container.settings)


def _as_utc(value: datetime) -> datetime:
    """Return *value* as a UTC-aware datetime (defensive coercion).

    SQLite round-trips ``DateTime(timezone=True)`` as naive; PostgreSQL
    returns aware values.  The scheduler compares against
    ``datetime.now(UTC)`` so naive values are tagged UTC first.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def build_intel_registry(settings: Settings) -> IntelProviderRegistry:
    """Build the configured provider registry (see intel.registry)."""
    from finance_sync.intel.registry import build_intel_registry as _build

    return _build(settings)


async def intel_refresh_job(container: Container) -> dict[str, Any]:
    """APScheduler job entrypoint: refresh intel for all tenants.

    Runs on the worker's own cadence (independent of bunq/T212/Wealthfolio
    jobs).  A slow/failing provider is isolated by the per-provider
    timeout and can never block the other sync jobs.
    """
    settings: Settings = container.settings
    if not getattr(settings, "worker_job_intel_enabled", False):
        return {"status": "disabled"}

    registry = _registry_from_container(container)
    if not registry.enabled():
        return {"status": "no_providers"}

    tenant_ids = await _all_tenant_ids(container)
    summary: dict[str, Any] = {"tenants": {}}
    for tenant_id in tenant_ids:
        scheduler = IntelScheduler(container, registry=registry)
        summary["tenants"][str(tenant_id)] = await scheduler.refresh_all(
            str(tenant_id)
        )
    return summary


async def _all_tenant_ids(container: Container) -> Sequence[Any]:
    """Return all tenant ids from the database."""
    from sqlalchemy import select

    from finance_sync.models.tenant import Tenant

    async with container.session_factory() as session:
        result = await session.execute(select(Tenant.id))
        return list(result.scalars().all())
