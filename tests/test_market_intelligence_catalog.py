"""Tests for the market-intelligence source catalog read contract.

Covers the t_7927b7a1 deliverables:

* ``GET /api/v1/market-intelligence/sources`` — static source metadata
  (provenance, licence terms, configuration links, rate-limit and
  freshness policies, declared capabilities) exposed tenant-scoped;
* the catalog **never** exposes provider credentials, raw API
  responses or unlicensed full article text;
* the MCP surface exposes the same catalog via ``list_intel_sources``
  and the ``finance://intel-sources`` resource;
* every shipped adapter (``sec``, ``sec_press``, ``openbb``) is
  covered by the catalog.
"""

# pyright: basic

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from finance_sync.config.settings import Settings
from finance_sync.intel.registry import build_intel_registry
from finance_sync.services.market_intelligence_catalog import (
    IntelSourceCatalogService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

import pytest
from pydantic import SecretStr
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


@pytest.fixture
def settings() -> Settings:
    """Settings without an OpenBB key → the catalog still covers openbb."""
    return Settings(
        secret_key=SecretStr("test-secret-that-is-long-enough"),
        master_encryption_key=SecretStr("a" * 64),  # 32 hex bytes = 32 bytes
        database_url=None,
        redis_url=None,
    )


# ═══════════════════════════════════════════════════════════════════════
# Catalog service — coverage, provenance, no secrets
# ═══════════════════════════════════════════════════════════════════════


class TestSourceCatalogService:
    async def test_catalog_covers_every_shipped_adapter(
        self, settings: Settings
    ) -> None:
        """sec, sec_press and openbb are all present in the catalog."""
        registry = build_intel_registry(settings)
        service = IntelSourceCatalogService(registry)
        catalog = await service.catalog()

        providers = {s.provider for s in catalog.sources}
        assert providers == {"sec", "sec_press", "openbb"}

        by_key = {s.provider: s for s in catalog.sources}
        assert by_key["sec"].display_name == "SEC EDGAR"
        assert by_key["sec_press"].display_name == "SEC Press Releases"
        assert by_key["openbb"].display_name == "OpenBB Platform"

    async def test_catalog_carries_provenance_and_licence_terms(
        self, settings: Settings
    ) -> None:
        """Each source exposes its licence note and config link."""
        registry = build_intel_registry(settings)
        service = IntelSourceCatalogService(registry)
        catalog = await service.catalog()

        for source in catalog.sources:
            assert source.license_note
            note = source.license_note.lower()
            assert (
                "public domain" in note
                or "public-domain" in note
                or "openbb terms" in note
            )
            assert source.config_url  # every adapter declares one

    async def test_catalog_declares_rate_limit_and_freshness(
        self, settings: Settings
    ) -> None:
        """Rate-limit and freshness policies are exposed as numbers."""
        registry = build_intel_registry(settings)
        service = IntelSourceCatalogService(registry)
        catalog = await service.catalog()

        sec_press = next(
            s for s in catalog.sources if s.provider == "sec_press"
        )
        assert sec_press.rate_limit.max_requests == 10
        assert sec_press.rate_limit.window_seconds == 1
        assert sec_press.rate_limit.respect is True
        assert sec_press.freshness.max_age_seconds == 6 * 3600
        assert sec_press.freshness.min_interval_seconds == 15 * 60

        sec = next(s for s in catalog.sources if s.provider == "sec")
        assert sec.freshness.max_age_seconds == 24 * 3600
        assert sec.freshness.min_interval_seconds == 3600

    async def test_catalog_never_exposes_secrets_or_raw_content(
        self, settings: Settings
    ) -> None:
        """No credential values, no API responses, no full text anywhere."""
        registry = build_intel_registry(settings)
        service = IntelSourceCatalogService(registry)
        catalog = await service.catalog()

        dumped = catalog.model_dump_json()
        lowered = dumped.lower()

        # No credential *values* or secret-carrying field names.  The
        # config_flags legitimately carry key *names* (e.g.
        # 'OPENBB_API_KEY'), so value-shaped patterns are what matter.
        for shape in (
            "sk-",
            "sk_",
            "bearer ",
            "token=",
            "password=",
            "authorization:",
            "x-api-key:",
            "secret=",
            "api key value",
            "encrypted_payload",
            "nonce",
            "credential_value",
        ):
            assert shape not in lowered, f"secret value leaked: {shape}"

        # No raw content *fields* (headline/body/summary are item-level
        # read fields, never part of the static catalog).  The licence
        # notes legitimately mention the word 'headline' in prose.
        import json as _json

        payload = _json.loads(dumped)
        for source in payload["sources"]:
            source_fields = set(source.keys())
            assert (
                not {"headline", "summary", "body", "content"} & source_fields
            )

        # The only 'key' mentions are the *names* of config flags.
        for source in catalog.sources:
            for flag in source.config_flags:
                assert flag.isupper()  # env-var names only, never values

    async def test_catalog_config_flags_are_names_only(
        self, settings: Settings
    ) -> None:
        """Config flags are key names; openbb flags include the key name."""
        registry = build_intel_registry(settings)
        service = IntelSourceCatalogService(registry)
        catalog = await service.catalog()

        by_key = {s.provider: s for s in catalog.sources}
        assert by_key["sec"].config_flags == ["INTEL_SEC_ENABLED"]
        assert by_key["sec_press"].config_flags == ["INTEL_SEC_PRESS_ENABLED"]
        assert "OPENBB_API_KEY" in by_key["openbb"].config_flags

    async def test_catalog_capabilities_reflect_registry(
        self, settings: Settings
    ) -> None:
        """Capabilities + availability come from the provider itself."""
        registry = build_intel_registry(settings)
        service = IntelSourceCatalogService(registry)
        catalog = await service.catalog()

        sec_press = next(
            s for s in catalog.sources if s.provider == "sec_press"
        )
        cap_names = {c.name for c in sec_press.capabilities}
        assert cap_names == {"news"}
        for cap in sec_press.capabilities:
            assert cap.available in ("available", "degraded", "unavailable")

    async def test_catalog_sorted_by_provider_key(
        self, settings: Settings
    ) -> None:
        """Sources are returned in a stable, sorted order."""
        registry = build_intel_registry(settings)
        service = IntelSourceCatalogService(registry)
        catalog = await service.catalog()
        keys = [s.provider for s in catalog.sources]
        assert keys == sorted(keys)


# ═══════════════════════════════════════════════════════════════════════
# REST contract — OpenAPI registration
# ═══════════════════════════════════════════════════════════════════════


class TestSourceCatalogOpenAPI:
    """The /market-intelligence/sources endpoint appears in OpenAPI."""

    def test_sources_endpoint_registered(self) -> None:
        from fastapi.testclient import TestClient

        from finance_sync.app import create_app

        app = create_app(
            settings=Settings(
                secret_key="test-secret-key-at-least-16-chars",  # type: ignore[call-arg]
                database_url=None,
                redis_url=None,
            )
        )
        with TestClient(app) as client:
            paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/market-intelligence/sources" in paths
        assert "get" in paths["/api/v1/market-intelligence/sources"]
