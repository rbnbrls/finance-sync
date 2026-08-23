"""Scheduled-run dispatcher for tenant sync schedules.

The worker no longer relies on global per-provider intervals for *when a
tenant's connection is synced*: that decision moved to the tenant-scoped
``sync_schedules`` rows.  This module is the worker-side counterpart of
the API/service layer:

* every tick it selects **enabled** schedules whose ``next_run_at`` is
  due (``<= now``),
* claims each schedule **atomically** with a guarded ``UPDATE ... WHERE
  last_scheduled_at < claim_cutoff`` so multiple worker replicas,
  scheduler restarts and misfires can never double-start the same
  scheduled execution (the claim update is the idempotency key),
* executes the run through the existing connector/exporter flows
  (``SyncOrchestrator`` / ``WealthfolioExporter``), respecting provider
  rate limits (the connectors' ``RateLimiter``) and the operational
  feature flags (``worker_job_*_enabled`` remain limits, not user
  settings),
* coalesces misfires: a schedule more than ``CATCHUP_MAX_DELAY_DAYS``
  overdue is reset to the next future instant instead of firing a
  catch-up; otherwise at most one catch-up run happens per schedule per
  tick.

The global ``WORKER_JOB_BUNQ_SYNC_*`` / ``WORKER_JOB_TRADING212_SYNC_*``
/ ``WORKER_JOB_EXPORT_*`` settings now act as operational *gates*: when
a global job is disabled the corresponding schedules are not executed
(but remain enabled — re-enabling the gate resumes them).

Because the claim is a plain row update with a WHERE guard, it works on
PostgreSQL **and** on the aiosqlite test harness — no advisory locks, no
Redis dependency.
"""

from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, update

from finance_sync.config.settings import secret_value
from finance_sync.models import Account, Credential, ExportTarget, SyncSchedule
from finance_sync.models.export_target import TARGET_ACTIVE
from finance_sync.models.sync_schedule import (
    EXPORT_SCHEDULABLE_EXPORTERS,
    SCOPE_EXPORT,
    SCOPE_INGESTION,
)
from finance_sync.services.auth import decrypt_credential
from finance_sync.services.sync_schedule import (
    CATCHUP_MAX_DELAY_DAYS,
    compute_next_run,
)

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings
    from finance_sync.container import Container

logger = structlog.get_logger("finance_sync.worker.schedule_runner")

#: How overdue a schedule must be before the runner skips the catch-up
#: and simply resets ``next_run_at`` to the next future instant.
CATCHUP_MAX_DELAY = timedelta(days=CATCHUP_MAX_DELAY_DAYS)

#: Claim guard: a schedule is claimable when it was never scheduled, or
#: its last scheduled run started before ``now - claim_grace``.  The
#: grace period lets a long-running run (rate-limited provider) finish
#: without a second replica claiming the same window.
CLAIM_GRACE = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(value: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC (SQLite returns naive DATETIME).

    PostgreSQL's ``timestamptz`` always round-trips aware, so this is a
    no-op in production; it exists so the runner cannot crash on a naive
    value (the misfire check compares against the aware ``now``).
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def _load_due_schedules(
    session: Any,
    *,
    now: datetime,
) -> list[SyncSchedule]:
    """Return enabled schedules with ``next_run_at`` due or in the past."""
    stmt = (
        select(SyncSchedule)
        .where(
            SyncSchedule.enabled.is_(True),
            SyncSchedule.next_run_at.is_not(None),
            SyncSchedule.next_run_at <= now,
        )
        .order_by(SyncSchedule.next_run_at.asc())
        .limit(200)
    )
    rows = await session.scalars(stmt)
    result = list(rows.all())
    for row in result:
        row.next_run_at = _ensure_aware(row.next_run_at)
    return result


async def _claim_schedule(
    session: Any,
    *,
    schedule_id: str,
    claim_time: datetime,
    claim_cutoff: datetime,
) -> bool:
    """Atomically claim *schedule_id* for a run.

    The guarded UPDATE is the idempotency key: exactly one replica (or
    one retry) can win the ``WHERE last_scheduled_at < claim_cutoff``
    race per window.  Returns True when this caller won the claim.
    """
    result = await session.execute(
        update(SyncSchedule)
        .where(
            SyncSchedule.id == schedule_id,
            SyncSchedule.enabled.is_(True),
            (
                SyncSchedule.last_scheduled_at.is_(None)
                | (SyncSchedule.last_scheduled_at < claim_cutoff)
            ),
        )
        .values(
            last_scheduled_at=claim_time,
            updated_at=claim_time,
        )
    )
    return bool(result.rowcount and result.rowcount > 0)


async def _reset_next_run(
    session: Any,
    *,
    schedule_id: str,
    now: datetime,
) -> None:
    """Recompute ``next_run_at`` (post-run or after a skipped catch-up)."""
    stmt = select(SyncSchedule).where(SyncSchedule.id == schedule_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None or not row.enabled:
        return
    instants = compute_next_run(row, after=now, count=1)
    row.next_run_at = instants[0] if instants else None
    row.updated_at = _now()
    await session.flush()


async def _run_ingestion(
    container: Container,
    *,
    schedule: SyncSchedule,
) -> dict[str, Any]:
    """Run one ingestion schedule via the existing connector flow."""
    from finance_sync.api.v1.sync import _run_connection_sync

    settings: Settings = container.settings
    # Operational gate: the historical global job flags remain limits.
    provider_gate = {
        "bunq": settings.worker_job_bunq_sync_enabled,
        "trading212": settings.worker_job_trading212_sync_enabled,
    }
    # Resolve the connection; the schedule's target_id is the Credential
    # id (connection id).  A dangling schedule (connection deleted while
    # the schedule was not cleaned up) is skipped and reset so the worker
    # never plans against a dead source.
    async with container.session_factory() as session:
        session.info["settings"] = settings
        cred = (
            await session.execute(
                select(Credential).where(
                    Credential.id == schedule.target_id,
                    Credential.tenant_id == schedule.tenant_id,
                )
            )
        ).scalar_one_or_none()
    if cred is None:
        logger.warning(
            "schedule_dangling_connection",
            schedule_id=schedule.id,
            tenant_id=schedule.tenant_id,
            connection_id=schedule.target_id,
        )
        return {"status": "skipped", "reason": "connection_missing"}

    provider = cred.provider_key
    if provider_gate.get(provider) is False:
        logger.info(
            "schedule_gate_disabled",
            schedule_id=schedule.id,
            provider=provider,
        )
        return {"status": "skipped", "reason": "global_gate_disabled"}

    # Paused connections are skipped by the scheduler (mirrors the
    # historical behaviour); manual syncs remain possible.
    from finance_sync.models.credential import CONNECTION_STATUS_PAUSED

    if (cred.status or "active") == CONNECTION_STATUS_PAUSED:
        logger.info(
            "schedule_connection_paused",
            schedule_id=schedule.id,
            connection_id=str(cred.id),
        )
        return {"status": "skipped", "reason": "paused"}

    async with container.session_factory() as session:
        session.info["settings"] = settings
        link = await _run_connection_sync(
            container,
            session,
            str(schedule.tenant_id),
            cred,
        )
        await session.commit()
    return {
        "status": link.status,
        "error": link.error_message,
    }


async def run_export(
    container: Container,
    *,
    schedule: SyncSchedule,
) -> dict[str, Any]:
    """Run one export schedule via the existing exporter flow."""
    settings: Settings = container.settings
    exporter_key, separator, target_id = schedule.target_id.partition(":")
    if exporter_key not in EXPORT_SCHEDULABLE_EXPORTERS:
        return {"status": "skipped", "reason": "unknown_exporter"}

    target: ExportTarget | None = None
    target_secret: dict[str, Any] = {}
    if separator:
        async with container.session_factory() as session:
            target = await session.scalar(
                select(ExportTarget).where(
                    ExportTarget.id == target_id,
                    ExportTarget.tenant_id == schedule.tenant_id,
                    ExportTarget.target_type == exporter_key,
                )
            )
        if target is None or target.status != TARGET_ACTIVE:
            return {"status": "skipped", "reason": "target_inactive"}
        if not target.encrypted_secret or not target.secret_nonce:
            return {"status": "skipped", "reason": "target_unconfigured"}
        target_secret = json.loads(
            decrypt_credential(
                target.encrypted_secret, target.secret_nonce, settings
            )
        )

    if exporter_key == "wealthfolio":
        # Reuse the historical sweep gate only for the legacy, environment-
        # configured target. Destination rows have their own credentials and
        # are already operationally gated by their active schedule; requiring
        # the legacy global flag here makes every wizard-created destination
        # silently skip when WEALTHFOLIO_SERVER_URL/PASSWORD are unset.
        if not target and not settings.worker_job_export_enabled:
            return {"status": "skipped", "reason": "global_gate_disabled"}
        if not target and (
            not settings.wealthfolio_server_url
            or not secret_value(settings.wealthfolio_password)
        ):
            return {"status": "skipped", "reason": "target_unconfigured"}

        from finance_sync.exporter.wealthfolio.client import (
            WealthfolioClient,
            WealthfolioClientConfig,
        )
        from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
        from finance_sync.exporter.wealthfolio.exporter import (
            WealthfolioExporter,
        )

        wf_config = WealthfolioConfig.from_settings(settings)
        if target:
            # Only non-secret mapper preferences are allowed in the target.
            wf_config = WealthfolioConfig.model_validate(
                {
                    **wf_config.model_dump(),
                    **{
                        key: value
                        for key, value in target.configuration.items()
                        if key in WealthfolioConfig.model_fields
                    },
                }
            )
        wf_client = WealthfolioClient(
            config=WealthfolioClientConfig(
                base_url=(
                    target.configuration["server_url"]
                    if target
                    else settings.wealthfolio_server_url
                ),
                password=(
                    target_secret["password"]
                    if target
                    else secret_value(settings.wealthfolio_password)
                ),
            ),
        )
        await wf_client.authenticate()
        exporter = WealthfolioExporter(
            session_factory=container.session_factory,
            wf_config=wf_config,
            tenant_id=str(schedule.tenant_id),
            target_id=(str(target.id) if target else "legacy"),
        )
        accounts = None
        if target and target.selected_account_ids:
            async with container.session_factory() as session:
                accounts = list(
                    (
                        await session.scalars(
                            select(Account).where(
                                Account.tenant_id == schedule.tenant_id,
                                Account.id.in_(target.selected_account_ids),
                            )
                        )
                    ).all()
                )
        result = await exporter.push_to_wealthfolio(
            wf_client=wf_client, accounts=accounts
        )
        errors = list(result.get("errors") or [])
        status = "failed" if errors or result.get("failed", 0) else "completed"
        error = str(errors[0].get("error", "Export failed")) if errors else None
        return {"status": status, "error": error}

    # actual-budget: run the export cycle through its exporter.
    if exporter_key == "actual-budget":
        from finance_sync.exporter.actual_budget.config import (
            ActualBudgetConfig,
        )
        from finance_sync.exporter.actual_budget.exporter import (
            ActualBudgetExporter,
        )

        if target:
            ab_config = ActualBudgetConfig(
                server_url=target.configuration["server_url"],
                password=target_secret["password"],
                budget_name=target.configuration.get("budget_name") or None,
                sync_id=target.configuration.get("sync_id") or None,
                encryption_password=target_secret.get("encryption_password"),
                default_off_budget=bool(
                    target.configuration.get("default_off_budget", False)
                ),
            )
        else:
            ab_config = ActualBudgetConfig.from_settings(settings)
        exporter = ActualBudgetExporter(
            session_factory=container.session_factory,
            ab_config=ab_config,
            tenant_id=str(schedule.tenant_id),
            target_id=(str(target.id) if target else "legacy"),
        )
        result = await exporter.run_export(
            account_ids=(target.selected_account_ids if target else None)
        )
        return {
            "status": getattr(result, "status", "completed") or "completed",
            "error": getattr(result, "error_message", None),
        }

    # All remaining exporters are persisted, tenant-scoped targets.  The
    # legacy environment-based schedules only exist for Wealthfolio and
    # Actual Budget above.
    if target is None:
        return {"status": "skipped", "reason": "target_missing"}

    if exporter_key == "firefly":
        from finance_sync.exporter.firefly.config import FireflyConfig
        from finance_sync.exporter.firefly.exporter import FireflyExporter

        config = FireflyConfig(
            server_url=str(target.configuration["server_url"]),
            access_token=str(target_secret.get("access_token") or ""),
            verify_ssl=bool(target.configuration.get("verify_ssl", True)),
            request_timeout=float(
                target.configuration.get("request_timeout", 60.0)
            ),
            default_currency=str(
                target.configuration.get("default_currency", "EUR")
            ),
            import_tag=str(
                target.configuration.get("import_tag", "finance-sync")
            ),
        )
        result = await FireflyExporter(
            session_factory=container.session_factory,
            firefly_config=config,
            tenant_id=str(schedule.tenant_id),
        ).run_export(account_ids=target.selected_account_ids or None)
        return {"status": result.status, "error": result.error_message}

    if exporter_key in {"ghostfolio", "investbrain"}:
        if exporter_key == "ghostfolio":
            from finance_sync.exporter.ghostfolio.client import GhostfolioClient
            from finance_sync.exporter.ghostfolio.config import GhostfolioConfig
            from finance_sync.exporter.ghostfolio.exporter import (
                GhostfolioExporter,
            )

            config = GhostfolioConfig(
                server_url=str(target.configuration["server_url"]),
                access_token=str(target_secret.get("access_token") or ""),
                verify_ssl=bool(target.configuration.get("verify_ssl", True)),
                request_timeout=float(
                    target.configuration.get("request_timeout", 60.0)
                ),
                data_source=str(
                    target.configuration.get("data_source", "YAHOO")
                ),
                include_pending=bool(
                    target.configuration.get("include_pending", False)
                ),
            )
            async with GhostfolioClient(config) as client:
                result = await GhostfolioExporter(
                    session_factory=container.session_factory,
                    config=config,
                    tenant_id=str(schedule.tenant_id),
                ).run_export(
                    client,
                    account_ids=target.selected_account_ids or None,
                )
            return {
                "status": str(result.get("status", "failed")),
                "error": (
                    str(result.get("failures", [None])[0])
                    if result.get("failures")
                    else None
                ),
            }

        from finance_sync.exporter.investbrain.client import InvestBrainClient
        from finance_sync.exporter.investbrain.config import InvestBrainConfig
        from finance_sync.exporter.investbrain.exporter import (
            InvestBrainExporter,
        )

        config = InvestBrainConfig(
            server_url=str(target.configuration["server_url"]),
            access_token=str(target_secret.get("access_token") or ""),
            verify_ssl=bool(target.configuration.get("verify_ssl", True)),
            request_timeout=float(
                target.configuration.get("request_timeout", 60.0)
            ),
            include_pending=bool(
                target.configuration.get("include_pending", False)
            ),
            portfolio_name_prefix=str(
                target.configuration.get(
                    "portfolio_name_prefix", "finance-sync"
                )
            ),
        )
        async with InvestBrainClient(config) as client:
            result = await InvestBrainExporter(
                session_factory=container.session_factory,
                config=config,
                tenant_id=str(schedule.tenant_id),
            ).run_export(
                client,
                account_ids=target.selected_account_ids or None,
            )
        return {
            "status": str(result.get("status", "failed")),
            "error": (
                str(result.get("failures", [None])[0])
                if result.get("failures")
                else None
            ),
        }

    if exporter_key == "securo":
        from finance_sync.exporter.securo.config import SecuroConfig
        from finance_sync.exporter.securo.exporter import SecuroExporter

        config = SecuroConfig(
            server_url=str(target.configuration["server_url"]),
            email=str(target.configuration.get("email") or ""),
            password=str(target_secret.get("password") or ""),
            output_dir=Path(
                target.configuration.get("output_dir")
                or "/tmp/finance_sync_securo_exports"
            ),
            auto_create_accounts=bool(
                target.configuration.get("auto_create_accounts", True)
            ),
        )
        result = await SecuroExporter(
            container.session_factory,
            config,
            str(schedule.tenant_id),
        ).run_export(
            account_ids=target.selected_account_ids or None,
            push=True,
        )
        return {"status": result.status, "error": result.error_message}

    return {"status": "skipped", "reason": "unknown_exporter"}


async def run_due_schedules(container: Container) -> dict[str, Any]:
    """Claim and execute every due schedule; never raises.

    Returns a summary dict for the job monitor.  Each schedule is
    handled independently: a failing connection/exporter never blocks
    the remaining schedules, and the claim update guarantees exactly one
    start per schedule window even across replicas/restarts.
    """
    settings: Settings = container.settings
    now = _now()
    results: list[dict[str, Any]] = []

    async with container.session_factory() as session:
        session.info["settings"] = settings
        due = await _load_due_schedules(session, now=now)
        if not due:
            return {"checked": 0, "due": 0, "results": []}
        # Claim cutoff: a schedule is claimable when its last scheduled
        # run started before ``now - CLAIM_GRACE`` (or never).
        claim_cutoff = now - CLAIM_GRACE

        for schedule in due:
            claimed = await _claim_schedule(
                session,
                schedule_id=str(schedule.id),
                claim_time=now,
                claim_cutoff=claim_cutoff,
            )
            if not claimed:
                # Another replica/retry won this window.
                results.append(
                    {
                        "schedule_id": str(schedule.id),
                        "scope": schedule.scope,
                        "status": "skipped",
                        "reason": "already_claimed",
                    }
                )
                continue

            # Persist the claim BEFORE executing: the guarded UPDATE is
            # the idempotency key against concurrent replicas/retries.
            # If the claim is left uncommitted in this session and the
            # run opens its own sessions (it does), the rollback on
            # session close would silently undo it — a second replica
            # ticking the same window would claim and run again.
            await session.commit()

            # Misfire coalescing: an overdue schedule older than the
            # catch-up window is reset, not run.
            due_delay = now - (schedule.next_run_at or now)
            if due_delay > CATCHUP_MAX_DELAY:
                logger.info(
                    "schedule_misfire_reset",
                    schedule_id=str(schedule.id),
                    overdue_days=due_delay.days,
                )
                await _reset_next_run(
                    session, schedule_id=str(schedule.id), now=now
                )
                await session.commit()
                results.append(
                    {
                        "schedule_id": str(schedule.id),
                        "scope": schedule.scope,
                        "status": "skipped",
                        "reason": "misfire_reset",
                    }
                )
                continue

            try:
                if schedule.scope == SCOPE_INGESTION:
                    outcome = await _run_ingestion(container, schedule=schedule)
                elif schedule.scope == SCOPE_EXPORT:
                    outcome = await run_export(container, schedule=schedule)
                else:
                    outcome = {"status": "skipped", "reason": "unknown_scope"}
            except Exception as exc:  # never block sibling schedules
                outcome = {
                    "status": "failed",
                    "error": str(exc)[:500],
                }
                logger.error(
                    "schedule_run_failed",
                    schedule_id=str(schedule.id),
                    scope=schedule.scope,
                    error=traceback.format_exc()[-2000:],
                )

            # Record outcome + recompute next run atomically with the claim.
            async with container.session_factory() as session:
                session.info["settings"] = settings
                row = (
                    await session.execute(
                        select(SyncSchedule).where(
                            SyncSchedule.id == schedule.id
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.last_run_at = now
                    row.last_run_status = str(
                        outcome.get("status", "completed")
                    )[:16]
                    row.last_run_error = (
                        str(outcome.get("error") or "")[:500] or None
                    )
                    if row.enabled:
                        instants = compute_next_run(row, after=now, count=1)
                        row.next_run_at = instants[0] if instants else None
                    else:
                        row.next_run_at = None
                    row.updated_at = now
                await session.commit()

            results.append(
                {
                    "schedule_id": str(schedule.id),
                    "scope": schedule.scope,
                    "target": schedule.target_id,
                    "status": outcome.get("status", "completed"),
                }
            )

    return {"checked": len(due), "due": len(due), "results": results}


async def run_scheduled_syncs_job(container: Container) -> dict[str, Any]:
    """APScheduler entrypoint: dispatch due tenant schedules.

    Registered as a minute-tick job in ``worker/scheduler.py``.  The
    global ``WORKER_JOB_*`` flags remain operational gates — see module
    docstring.
    """
    log = logger.bind()
    try:
        summary = await run_due_schedules(container)
        log.info(
            "scheduled_syncs_tick_complete",
            checked=summary.get("checked", 0),
            due=summary.get("due", 0),
        )
        return summary
    except Exception:
        tb = traceback.format_exc()
        log.error("scheduled_syncs_tick_failed", error=tb[-2000:])
        raise
