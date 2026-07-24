"""Dependency injection container for finance-sync.

Stores initialised infrastructure objects (engine, Redis pool, etc.) and
provides factory methods that FastAPI route handlers access via
``Depends()``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from finance_sync.config.settings import Settings
    from finance_sync.db.uow import UnitOfWork
    from finance_sync.enrichment.gateway import EnrichmentGateway
    from finance_sync.enrichment.metadata_enricher import MetadataEnricher
    from finance_sync.enrichment.price_store import PriceStore
    from finance_sync.enrichment.security_resolver import SecurityResolver
    from finance_sync.identity.resolver import IdentityResolutionService
    from finance_sync.services.fx_service import FxService


class Container:
    """Holds initialised infrastructure objects.

    Usage
    -----
    1. ``Container.from_settings(settings)`` during startup.
    2. Store the container on ``app.state``.
    3. Route handlers retrieve dependencies via ``dependencies`` module
       helpers.
    """

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._redis: object | None = None
        self._settings: Settings | None = None
        self._enrichment_gateway: EnrichmentGateway | None = None
        self._price_store: PriceStore | None = None
        self._security_resolver: SecurityResolver | None = None
        self._metadata_enricher: MetadataEnricher | None = None
        self._identity_resolution_service: IdentityResolutionService | None = (
            None
        )
        self._fx_service: FxService | None = None

    # ── Initialisation ───────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: Settings) -> Container:
        """Build a container from a settings object."""
        container = cls()
        container._settings = settings

        if settings.database_url:
            database_url = settings.database_url.unicode_string()
            container._engine = create_async_engine(
                database_url,
                pool_size=settings.database_pool_min_size,
                max_overflow=(
                    settings.database_pool_max_size
                    - settings.database_pool_min_size
                ),
                echo=settings.is_debug,
            )
            container._session_factory = async_sessionmaker(
                bind=container._engine,
                expire_on_commit=False,
            )

        if settings.redis_url:
            import redis.asyncio as aioredis

            redis_url = settings.redis_url.unicode_string()
            container._redis = aioredis.from_url(
                redis_url,
                decode_responses=True,
            )

        return container

    # ── Properties ───────────────────────────────────────────────────

    @property
    def settings(self) -> Settings:
        """Return the settings object."""
        if self._settings is None:
            msg = "Container not initialised — call from_settings() first"
            raise RuntimeError(msg)
        return self._settings

    @property
    def engine(self) -> AsyncEngine:
        """Return the SQLAlchemy async engine."""
        if self._engine is None:
            msg = "Database engine not configured — set DATABASE_URL"
            raise RuntimeError(msg)
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the async session factory."""
        if self._session_factory is None:
            msg = "Database engine not configured — set DATABASE_URL"
            raise RuntimeError(msg)
        return self._session_factory

    @property
    def redis_client(self) -> object:
        """Return the Redis client.

        Use ``typing.cast()`` in the caller to get proper type inference::

            from typing import cast
            import redis.asyncio as aioredis
            r = cast(aioredis.Redis, container.redis_client)
        """
        if self._redis is None:
            msg = "Redis not configured — set REDIS_URL"
            raise RuntimeError(msg)
        return self._redis

    # ── Enrichment services ────────────────────────────────────────

    @property
    def enrichment_gateway(self) -> EnrichmentGateway:
        """Lazy-init the enrichment gateway."""
        if self._enrichment_gateway is None:
            from finance_sync.enrichment.gateway import (
                EnrichmentGateway,
            )

            self._enrichment_gateway = EnrichmentGateway(
                settings=self.settings,
                uow=self._make_uow(),
                price_store=self.price_store,
            )
        return self._enrichment_gateway

    @property
    def price_store(self) -> PriceStore:
        """Lazy-init the price store."""
        if self._price_store is None:
            from finance_sync.enrichment.price_store import (
                PriceStore,
            )

            self._price_store = PriceStore(
                session=self._get_session(),
                settings=self.settings,
            )
        return self._price_store

    @property
    def security_resolver(self) -> SecurityResolver:
        """Lazy-init the security resolver."""
        if self._security_resolver is None:
            from finance_sync.enrichment.security_resolver import (
                SecurityResolver,
            )

            self._security_resolver = SecurityResolver(
                uow=self._make_uow(),
                gateway=self.enrichment_gateway,
            )
        return self._security_resolver

    @property
    def metadata_enricher(self) -> MetadataEnricher:
        """Lazy-init the metadata enricher."""
        if self._metadata_enricher is None:
            from finance_sync.enrichment.metadata_enricher import (
                MetadataEnricher,
            )

            self._metadata_enricher = MetadataEnricher(
                uow=self._make_uow(),
                gateway=self.enrichment_gateway,
            )
        return self._metadata_enricher

    @property
    def identity_resolution_service(self) -> IdentityResolutionService:
        """Lazy-init the identity resolution service."""
        if self._identity_resolution_service is None:
            from finance_sync.identity.resolver import (
                IdentityResolutionService,
            )

            self._identity_resolution_service = IdentityResolutionService(
                uow=self._make_uow(),
                resolver=self.security_resolver,
                gateway=self.enrichment_gateway,
            )
        return self._identity_resolution_service

    @property
    def fx_service(self) -> FxService:
        """Lazy-init the FX service for exchange rate management."""
        if self._fx_service is None:
            from finance_sync.services.fx_service import FxService

            self._fx_service = FxService(
                settings=self.settings,
                uow=self._make_uow(),
            )
        return self._fx_service

    def _make_uow(self) -> UnitOfWork:
        """Create a UoW for the enrichment services."""
        from finance_sync.db.uow import UnitOfWork

        return UnitOfWork(self._get_session())

    def _get_session(self) -> AsyncSession:
        """Get a fresh async session."""
        return self.session_factory()  # type: ignore[return-value]

    # ── Lifespan helpers ─────────────────────────────────────────────

    @asynccontextmanager
    async def dispose(self) -> AsyncGenerator[None]:
        """Context manager that yields then tears down resources.

        Use inside a FastAPI lifespan handler::

            async with container.dispose():
                yield
        """
        try:
            yield
        finally:
            if self._engine is not None:
                await self._engine.dispose()
            if self._redis is not None:
                import redis.asyncio as aioredis  # noqa: TC002  # type: ignore[import]

                r: aioredis.Redis[bytes] = self._redis  # type: ignore[valid-type]
                await r.aclose()
            if self._enrichment_gateway is not None:
                await self._enrichment_gateway.close()
            if self._fx_service is not None:
                await self._fx_service.close()
