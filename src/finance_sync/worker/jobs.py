"""Scheduled job implementations for the worker process.

Each job is an async function that performs a specific task — syncing a
connector, enriching prices, processing the outbox, or reconciling data.
Jobs accept the DI container and optionally a job monitor.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.db.uow import UnitOfWork
from finance_sync.models.credential import Credential
from finance_sync.sync.orchestrator import SyncOrchestrator
from finance_sync.sync.outbox_publisher import OutboxPublisher

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from finance_sync.config.settings import Settings
    from finance_sync.container import Container
    from finance_sync.models import Tenant

logger = structlog.get_logger("finance_sync.worker.jobs")


# ── Retry helper ──────────────────────────────────────────────────────


async def retry_with_backoff(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    job_name: str = "unknown",
) -> Any:
    """Execute *coro_factory* with exponential backoff retry.

    The factory is called once per attempt to produce a fresh coroutine.
    Raises the last exception after all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "job_retrying",
                    job=job_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_s=delay,
                    error=str(exc)[:200],
                )
                await asyncio.sleep(delay)
    # All attempts exhausted
    msg = f"Job {job_name!r} failed after {max_attempts} attempts"
    raise JobRetryError(msg) from last_exc


class JobRetryError(Exception):
    """Raised when all retry attempts for a job are exhausted."""


def retryable_job(
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Callable[..., Any]:
    """Decorator that adds retry logic to an async job function.

    Usage::

        @retryable_job(max_attempts=3, base_delay=1.0)
        async def my_job(container: Container) -> dict[str, Any]:
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await retry_with_backoff(
                lambda: func(*args, **kwargs),
                max_attempts=max_attempts,
                base_delay=base_delay,
                job_name=func.__name__,
            )

        return wrapper

    return decorator


# ── Connector credential loading ──────────────────────────────────────


async def _get_tenant_credentials(
    uow: UnitOfWork,
    provider_key: str,
) -> list[tuple[Tenant, ConnectorConfig]]:
    """Load credentials for *provider_key* across all tenants.

    Returns a list of ``(tenant, connector_config)`` pairs.
    Credentials are decrypted on-the-fly.
    """
    tenants = await uow.tenants.list(limit=100)
    result: list[tuple[Tenant, ConnectorConfig]] = []

    for tenant in tenants:
        stmt = select(Credential).where(
            Credential.tenant_id == tenant.id,
            Credential.provider_key == provider_key,
        )
        cred_rows = await uow.session.execute(stmt)
        cred: Credential | None = cred_rows.scalar_one_or_none()

        if cred is None:
            continue

        # Decrypt the credential payload
        from finance_sync.services.auth import decrypt_credential

        try:
            decrypted = decrypt_credential(
                cred.encrypted_payload,
                cred.nonce,
                uow.session.info.get("settings"),
            )
        except Exception:
            logger.error(
                "credential_decrypt_failed",
                tenant_id=tenant.id,
                provider_key=provider_key,
            )
            continue

        credentials: dict[str, str] = json.loads(decrypted)
        config = ConnectorConfig(
            provider_type=provider_key,
            credentials=credentials,
        )
        result.append((tenant, config))

    return result


# ── Individual job functions ──────────────────────────────────────────


async def sync_connector_job(
    container: Container,
    provider_key: str,
    *,
    since_days: int = 90,
    max_attempts: int | None = None,
    base_delay: float | None = None,
) -> dict[str, Any]:
    """Sync a specific connector for all configured tenants.

    Returns a summary dict with per-tenant results.
    """
    settings: Settings = container.settings
    max_attempts = max_attempts or settings.worker_retry_max_attempts
    base_delay = base_delay or settings.worker_retry_base_delay_s

    registry = ConnectorRegistry()
    log = logger.bind(provider=provider_key)
    log.info("sync_job_starting")

    async with container.session_factory() as session:
        uow = UnitOfWork(session)
        # Attach settings so credential decryption can access them
        session.info["settings"] = settings

        configs = await _get_tenant_credentials(uow, provider_key)

    if not configs:
        log.info("sync_job_no_tenants")
        return {"provider": provider_key, "tenants_synced": 0, "results": []}

    summary: list[dict[str, Any]] = []

    for tenant, config in configs:
        tenant_log = log.bind(tenant_id=tenant.id)

        async def _run_single(
            _cfg: ConnectorConfig = config,
            _tenant: Tenant = tenant,
        ) -> dict[str, Any]:
            orchestrator = SyncOrchestrator(
                session_factory=container.session_factory,
                registry=registry,
                tenant_id=_tenant.id,
            )
            since = datetime.now(UTC) - timedelta(days=since_days)
            result = await orchestrator.run_sync(
                provider_type=_cfg.provider_type,
                config=_cfg,
                since=since,
            )
            return {
                "tenant_id": _tenant.id,
                "status": result.status.value,
                "accounts_synced": result.accounts_synced,
                "transactions_synced": result.transactions_synced,
                "duration_s": round(result.duration_s, 2),
                "error": result.error_message,
            }

        try:
            tenant_result = await retry_with_backoff(
                _run_single,
                max_attempts=max_attempts,
                base_delay=base_delay,
                job_name=f"sync_{provider_key}_{tenant.id[:8]}",
            )
            tenant_log.info(
                "sync_job_tenant_complete",
                **tenant_result,
            )
        except Exception as exc:
            tenant_result = {
                "tenant_id": tenant.id,
                "status": "failed",
                "accounts_synced": 0,
                "transactions_synced": 0,
                "duration_s": 0.0,
                "error": str(exc)[:500],
            }
            tenant_log.error("sync_job_tenant_failed", error=str(exc)[:300])

        summary.append(tenant_result)

    total_accounts = sum(r["accounts_synced"] for r in summary)
    total_transactions = sum(r["transactions_synced"] for r in summary)
    failed = [r for r in summary if r["status"] == "failed"]

    log.info(
        "sync_job_complete",
        tenants_synced=len(summary),
        total_accounts=total_accounts,
        total_transactions=total_transactions,
        failed=len(failed),
    )

    return {
        "provider": provider_key,
        "tenants_synced": len(summary),
        "total_accounts": total_accounts,
        "total_transactions": total_transactions,
        "failed": len(failed),
        "results": summary,
    }


async def sync_bunq_job(container: Container) -> dict[str, Any]:
    """Sync all bunq connectors."""
    return await sync_connector_job(container, "bunq")


async def sync_trading212_job(container: Container) -> dict[str, Any]:
    """Sync all Trading212 connectors."""
    return await sync_connector_job(container, "trading212")


async def enrich_prices_job(container: Container) -> dict[str, Any]:
    """Enrich security prices for all securities that need fresh data.

    Runs every 15 minutes during market hours (9:30-16:00 EST).  Fetches
    latest quotes for all tracked securities and stores them as price
    observations.
    """
    log = logger.bind()
    log.info("enrich_prices_job_starting")

    gateway = container.enrichment_gateway
    async with container.session_factory() as session:
        from finance_sync.db.uow import UnitOfWork as _UoW

        uow = _UoW(session)
        securities = await uow.securities.list(limit=200)

        enriched = 0
        failed = 0
        for security in securities:
            # Determine the best identifier to use for the quote lookup
            identifier: str | None = None
            id_type: str = "ticker"
            if security.ticker:
                identifier = security.ticker
            elif security.figi:
                identifier = security.figi
                id_type = "figi"
            elif security.isin:
                identifier = security.isin
                id_type = "isin"

            if not identifier:
                continue

            try:
                quote = await gateway.get_latest_quote(
                    security_id=str(security.id),
                    identifier=identifier,
                    identifier_type=id_type,
                )
                if quote is not None:
                    enriched += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                log.debug(
                    "enrich_quote_failed",
                    security_id=str(security.id),
                    identifier=identifier,
                )

        await session.commit()

    log.info(
        "enrich_prices_job_complete",
        enriched=enriched,
        failed=failed,
    )
    return {"enriched": enriched, "failed": failed}


async def nightly_reconciliation_job(container: Container) -> dict[str, Any]:
    """Nightly full reconciliation: re-sync all connectors for all tenants.

    This is a heavy job that re-fetches all transactions from all
    configured connectors and reconciles them against the local data store.
    """
    log = logger.bind()
    log.info("reconciliation_job_starting")

    results: list[dict[str, Any]] = []

    try:
        # Re-sync bunq
        bunq_result = await sync_bunq_job(container)
        results.append(bunq_result)
    except Exception as exc:
        results.append({"provider": "bunq", "error": str(exc)[:300]})
        log.error("reconciliation_bunq_failed", error=str(exc)[:200])

    try:
        t212_result = await sync_trading212_job(container)
        results.append(t212_result)
    except Exception as exc:
        results.append({"provider": "trading212", "error": str(exc)[:300]})
        log.error("reconciliation_t212_failed", error=str(exc)[:200])

    # Prune old price data during nightly reconciliation
    try:
        async with container.session_factory() as session:
            from finance_sync.enrichment.price_store import PriceStore

            store = PriceStore(
                session=session,
                settings=container.settings,
            )
            pruned_minute = await store.prune_intraday_data()
            pruned_hour = await store.prune_hourly_data()
            await session.commit()
            log.info(
                "reconciliation_pruning_complete",
                pruned_minute=pruned_minute,
                pruned_hour=pruned_hour,
            )
            results.append(
                {
                    "pruned_minute_prices": pruned_minute,
                    "pruned_hourly_prices": pruned_hour,
                },
            )
    except Exception as exc:
        log.error("reconciliation_pruning_failed", error=str(exc)[:200])

    log.info("reconciliation_job_complete")
    return {"status": "completed", "results": results}


async def process_webhook_retries_job(container: Container) -> dict[str, Any]:
    """Retry failed webhook deliveries whose retry time has arrived.

    Runs periodically alongside the outbox consumer.
    """
    from finance_sync.services.webhook import WebhookService

    svc = WebhookService(
        session_factory=container.session_factory,
        settings=container.settings,
    )
    try:
        retried = await svc.retry_due_deliveries()
        logger.info("webhook_retry_job_complete", retried=retried)
        return {"retried": retried}
    except Exception:
        tb = traceback.format_exc()
        logger.error("webhook_retry_job_failed", error=tb[:500])
        raise
    finally:
        await svc.close()


async def process_outbox_job(container: Container) -> dict[str, Any]:
    """Process pending outbox messages.

    Runs every 30 seconds.  Dispatches pending outbox messages to
    registered handlers including webhooks.
    """
    log = logger.bind()
    publisher = OutboxPublisher(
        session_factory=container.session_factory,
        poll_interval=5.0,
        batch_size=50,
    )

    # Register webhook handler (catch-all — it filters internally)
    from finance_sync.services.webhook import WebhookService

    webhook_svc = WebhookService(
        session_factory=container.session_factory,
        settings=container.settings,
    )
    publisher.register_handler("*", webhook_svc.handle_outbox_message)

    try:
        processed = await publisher.run_once()
        log.info("outbox_job_complete", processed=processed)
        return {"processed": processed}
    except Exception:
        tb = traceback.format_exc()
        log.error("outbox_job_failed", error=tb[:500])
        raise
    finally:
        await webhook_svc.close()
