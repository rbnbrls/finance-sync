"""Application lifespan — initialise / tear down infrastructure."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text

from finance_sync.config.settings import Settings, secret_value
from finance_sync.container import Container

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI


logger = structlog.get_logger("finance_sync.lifespan")

_DB_RETRIES: int = 5
_DB_RETRY_DELAY_S: float = 2.0
_DB_RETRY_BACKOFF: float = 2.0


async def _init_database(container: Container) -> None:
    """Connect to the database and seed default tenant / admin user.

    Schema is owned exclusively by Alembic — this function never creates
    tables.  It only ensures the ``pgcrypto`` extension exists (needed for
    ``gen_random_uuid()`` used by migrations and seed inserts) and seeds
    the default tenant and admin user (idempotent).

    Retries with exponential backoff on transient failures so that a
    momentarily-unavailable database does not crash the whole app container.
    """
    last_exc: Exception | None = None

    for attempt in range(1, _DB_RETRIES + 1):
        try:
            async with container.engine.begin() as conn:
                # Enable pgcrypto extension (needed for gen_random_uuid())
                await conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS pgcrypto")
                )
                # ── Seed default tenant and admin user (idempotent) ────
                from datetime import UTC, datetime

                from finance_sync.services.auth import hash_password

                now = datetime.now(UTC)

                # Create default tenant if it doesn't exist
                tenant_row = await conn.execute(
                    text("SELECT id FROM tenants WHERE slug = 'default'")
                )
                tenant = tenant_row.first()
                if tenant is None:
                    tenant_id = await conn.execute(
                        text(
                            "INSERT INTO tenants "
                            "(id, slug, name, created_at, updated_at) "
                            "VALUES (gen_random_uuid(), 'default', "
                            "'Default Tenant', :now, :now) RETURNING id"
                        ),
                        {"now": now},
                    )
                    tid = tenant_id.scalar_one()
                    logger.info("created_default_tenant")
                else:
                    tid = tenant[0]

                # Create admin user if it doesn't exist
                user_row = await conn.execute(
                    text(
                        "SELECT id FROM users "
                        "WHERE email = 'admin@finance-sync.local'"
                    )
                )
                if user_row.first() is None:
                    pwd = hash_password("admin")
                    await conn.execute(
                        text(
                            "INSERT INTO users "
                            "(id, tenant_id, email, hashed_password, "
                            "display_name, role, is_active, "
                            "created_at, updated_at) "
                            "VALUES (gen_random_uuid(), :tid, "
                            " 'admin@finance-sync.local', :pwd, "
                            "'Admin', 'admin', true, :now, :now)"
                        ),
                        {"tid": tid, "pwd": pwd, "now": now},
                    )
                    logger.info(
                        "seeded_admin_user",
                        email="admin@finance-sync.local",
                    )
                else:
                    logger.info(
                        "admin_user_exists",
                        email="admin@finance-sync.local",
                    )

                # Staging starts with safe static connector configs. Users
                # may later switch each one to the endpoint-locked official
                # sandbox/demo API from the dashboard.
                if container.settings.is_staging:
                    from finance_sync.connectors.environment import (
                        STAGING_MANAGED_PROVIDERS,
                        staging_connector_config,
                    )
                    from finance_sync.services.auth import encrypt_credential

                    for provider in sorted(STAGING_MANAGED_PROVIDERS):
                        credentials, options = staging_connector_config(
                            provider, container.settings
                        )
                        encrypted, nonce = encrypt_credential(
                            json.dumps(credentials, separators=(",", ":")),
                            container.settings,
                        )
                        description = json.dumps(
                            {
                                **options,
                                "_label": "Staging connector",
                            },
                            separators=(",", ":"),
                        )
                        await conn.execute(
                            text(
                                "INSERT INTO credentials "
                                "(id, tenant_id, provider_key, "
                                "encrypted_payload, nonce, description, "
                                "created_at, updated_at) "
                                "VALUES (gen_random_uuid(), :tid, :provider, "
                                ":encrypted, :nonce, :description, :now, :now) "
                                "ON CONFLICT (tenant_id, provider_key) "
                                "WHERE tenant_id IS NOT NULL DO NOTHING"
                            ),
                            {
                                "tid": tid,
                                "provider": provider,
                                "encrypted": encrypted,
                                "nonce": nonce,
                                "description": description,
                                "now": now,
                            },
                        )
                    logger.info(
                        "seeded_staging_connectors",
                        providers=sorted(STAGING_MANAGED_PROVIDERS),
                    )
                await conn.commit()

            # Seed normalized, synthetic records for local development and
            # staging. Production is intentionally excluded in the caller.
            if not container.settings.is_production:
                from sqlalchemy import select

                from finance_sync.models import Tenant, User
                from finance_sync.services.non_production_seed import (
                    seed_non_production_dataset,
                )

                async with container.session_factory() as session:
                    tenant_id = await session.scalar(
                        select(Tenant.id).where(Tenant.slug == "default")
                    )
                    owner_user_id = await session.scalar(
                        select(User.id).where(
                            User.email == "admin@finance-sync.local"
                        )
                    )
                    if tenant_id is not None:
                        seeded = await seed_non_production_dataset(
                            session,
                            tenant_id,
                            str(owner_user_id) if owner_user_id else None,
                        )
                        logger.info(
                            "non_production_dataset_seeded"
                            if seeded
                            else "non_production_dataset_exists",
                            providers=["bunq", "trading212", "degiro_pension"],
                        )

            # Success — exit the retry loop
            logger.info("database_initialised", attempt=attempt)
            return

        except Exception as exc:
            last_exc = exc
            if attempt < _DB_RETRIES:
                delay = _DB_RETRY_DELAY_S * (_DB_RETRY_BACKOFF ** (attempt - 1))
                logger.warning(
                    "database_init_attempt_failed",
                    attempt=attempt,
                    max_retries=_DB_RETRIES,
                    retry_delay_s=round(delay, 1),
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "database_init_failed",
                    attempts=attempt,
                    error=str(exc),
                )

    # All retries exhausted — re-raise so the app crashes with a clear
    # message rather than silently serving with no schema.
    if last_exc is not None:
        raise last_exc


async def _bootstrap_legacy_export_targets(container: Container) -> None:
    """Migrate legacy global exporter settings into one stored target.

    This is deliberately a runtime bootstrap rather than an Alembic data
    migration: environment variables exist only in the deployment, not in the
    database.  It is idempotent and never creates a second target of a type
    once the owner has configured one in the destinations wizard.
    """
    from sqlalchemy import select

    from finance_sync.models import Tenant
    from finance_sync.models.export_target import (
        TARGET_ACTIVE,
        ExportTarget,
    )
    from finance_sync.models.sync_schedule import SCOPE_EXPORT
    from finance_sync.services.auth import encrypt_credential
    from finance_sync.services.sync_schedule import SyncScheduleService

    settings = container.settings
    candidates: list[tuple[str, str, dict[str, object], dict[str, str]]] = []
    wealthfolio_password = secret_value(settings.wealthfolio_password)
    actual_budget_password = secret_value(settings.actual_budget_password)
    if settings.wealthfolio_server_url and wealthfolio_password:
        candidates.append(
            (
                "wealthfolio",
                "Migrated Wealthfolio",
                {"server_url": settings.wealthfolio_server_url},
                {"password": wealthfolio_password},
            )
        )
    if settings.actual_budget_server_url and actual_budget_password:
        candidates.append(
            (
                "actual-budget",
                "Migrated Actual Budget",
                {
                    "server_url": settings.actual_budget_server_url,
                    "budget_name": settings.actual_budget_budget_name or "",
                    "sync_id": settings.actual_budget_sync_id or "",
                },
                {
                    "password": actual_budget_password,
                    "encryption_password": (
                        settings.actual_budget_encryption_password or ""
                    ),
                },
            )
        )
    if not candidates:
        return

    async with container.session_factory() as session:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.slug == "default")
        )
        if tenant is None:
            return
        for target_type, label, configuration, secret in candidates:
            existing = await session.scalar(
                select(ExportTarget).where(
                    ExportTarget.tenant_id == tenant.id,
                    ExportTarget.target_type == target_type,
                )
            )
            if existing is not None:
                continue
            encrypted, nonce = encrypt_credential(
                json.dumps(secret, separators=(",", ":")), settings
            )
            target = ExportTarget(
                tenant_id=tenant.id,
                target_type=target_type,
                display_name=label,
                status=TARGET_ACTIVE,
                configuration=configuration,
                encrypted_secret=encrypted,
                secret_nonce=nonce,
            )
            session.add(target)
            await session.flush()
            schedule = await SyncScheduleService(session).ensure_for_scope(
                str(tenant.id),
                scope=SCOPE_EXPORT,
                target_id=f"{target_type}:{target.id}",
            )
            target.schedule_id = schedule.id
        await session.commit()
    logger.info("legacy_export_targets_bootstrapped")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """FastAPI lifespan context manager.

    Startup
    -------
    * Use the settings already stored on ``app.state`` (set by
      ``create_app``) or load from environment / ``.env``.
    * Build the DI container (DB engine, Redis pool).
    * Store the container on ``app.state`` so route handlers can access
      it via ``request.app.state.container``.
    * Ensure the database is reachable and seed default data.  The schema
      itself is owned by Alembic: operators run ``alembic upgrade head``
      (as part of the release pipeline) before the app starts — the
      lifespan never creates tables.

    Shutdown
    --------
    * Dispose the DB engine.
    * Close the Redis connection.
    """
    # If create_app already stored settings on state, use them
    settings: Settings = getattr(app.state, "_settings", None) or Settings()
    container = Container.from_settings(settings)

    # Store so route handlers can access via request.app.state.container
    app.state.container = container

    # -- Ensure database is migrated and seed default data --------------
    if settings.database_url is not None:
        await _init_database(container)
        await _bootstrap_legacy_export_targets(container)

    async with container.dispose():
        yield  # app runs here
