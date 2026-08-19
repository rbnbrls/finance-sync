"""Integration tests for the market-intelligence REST read contract.

Proves the t_7927b7a1 read-contract acceptance criteria against **real**
PostgreSQL through the full FastAPI HTTP stack (ASGI transport + JWT
auth + DI container):

* ``GET /api/v1/market-intelligence/sources`` — the static source
  catalog (provenance, licence terms, config flags, rate-limit and
  freshness policies) is served to an authenticated tenant and never
  contains credentials or raw content;
* ``GET /api/v1/market-intelligence/items`` — observations are
  tenant-scoped: tenant B can never read tenant A's record
  (cross-tenant item id → 404), and restricted items are served
  without a body;
* ``GET /api/v1/market-intelligence/providers`` — per-provider state
  with sanitised errors (never credentials).
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from finance_sync.db.uow import UnitOfWork
from finance_sync.intel.adapters.sec_press import SecPressReleaseProvider
from finance_sync.intel.enums import IntelCapability, IntelLicenseClass
from finance_sync.intel.hashing import content_hash
from finance_sync.intel.models import IntelItem, IntelStructuredFact
from finance_sync.intel.service import IntelIngestionService
from finance_sync.models import Tenant, User
from finance_sync.models.enums import UserRole
from finance_sync.services.auth import create_access_token, hash_password

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.config.settings import Settings

pytestmark = pytest.mark.integration


async def _create_tenant_user(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    slug: str,
) -> dict[str, Any]:
    """Create a tenant + admin user, returning JWT headers."""
    async with session_factory() as session:
        tenant = Tenant(slug=slug, name="Intel Read Contract")
        session.add(tenant)
        await session.flush()
        user = User(
            email=f"{slug}@finance-sync.local",
            tenant_id=str(tenant.id),
            hashed_password=hash_password("test-password"),
            display_name="Intel Admin",
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        tenant_id = str(tenant.id)
        user_id = str(user.id)

    token = create_access_token(
        {"sub": user_id, "tenant_id": tenant_id, "role": "admin"},
        settings,
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _intel_item(
    *,
    provider: str,
    source_id: str,
    headline: str,
    license_class: IntelLicenseClass,
    body: str | None = None,
) -> IntelItem:
    now = datetime.now(UTC)
    return IntelItem(
        provider=provider,
        source_id=source_id,
        canonical_url=f"https://example.com/{source_id}",
        kind="news_article",  # type: ignore[arg-type]
        published_at=now,
        fetched_at=now,
        language="en",
        license_class=license_class,
        content_hash=content_hash(
            {"provider": provider, "source_id": source_id, "headline": headline}
        ),
        headline=headline,
        summary="Short summary",
        body=body,
        # Adapters never set store_full_text for restricted classes; the
        # licensing policy drops the body silently.  We pass the body
        # anyway to prove it can never reach a read contract.
        store_full_text=False,
        store_summary=True,
        identifiers={},
        facts=[IntelStructuredFact(key="eps_estimate", value="1.25")],
    )


class _NoopResolver:
    """Resolver double: never links, never queues."""

    async def resolve_by_isin(self, isin: str) -> None:
        del isin

    async def resolve_by_figi(self, figi: str) -> None:
        del figi

    async def resolve_by_ticker(self, ticker: str) -> None:
        del ticker


@pytest.fixture
def resolver() -> _NoopResolver:
    """No-op resolver double for ingestion runs."""
    return _NoopResolver()


async def _ingest(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    provider: str,
    items: list[IntelItem],
    resolver: Any | None = None,
) -> None:
    async with session_factory() as session:
        uow = UnitOfWork(session)
        service = IntelIngestionService(uow, resolver or _NoopResolver())  # type: ignore[arg-type]
        await service.ingest_items(tenant_id, provider, items)
        await uow.commit()


@pytest.fixture
def settings() -> Settings:
    """Test settings with fixed secret keys for deterministic JWTs."""
    from pydantic import SecretStr

    from finance_sync.config.settings import Settings as _Settings

    return _Settings(
        secret_key=SecretStr("test-secret-key-at-least-16-chars"),
        master_encryption_key=SecretStr("0123456789abcdef" * 4),
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def intel_container(
    pg_engine: Any,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Any:
    """DI container bound to the harness PG (like the e2e conftest)."""
    from finance_sync.container import Container

    container = Container.from_settings(settings)
    container._engine = pg_engine  # type: ignore[attr-defined]
    container._session_factory = session_factory  # type: ignore[attr-defined]
    return container


@pytest.fixture
def intel_app(intel_container: Any, settings: Settings) -> Any:
    """FastAPI app with the container attached (lifespan not run)."""
    from finance_sync.app import create_app

    app = create_app(settings=settings)
    app.state.container = intel_container
    return app


@pytest.fixture
async def intel_client(intel_app: Any) -> Any:
    """Async HTTP client against the in-process FastAPI app."""
    transport = httpx.ASGITransport(app=intel_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://intel"
    ) as client:
        yield client


@pytest.fixture
async def seeded_read_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> dict[str, Any]:
    """Tenant A (with data) and tenant B (empty) with JWT headers."""
    tenant_a = await _create_tenant_user(
        session_factory, settings, slug="intel-a"
    )
    tenant_b = await _create_tenant_user(
        session_factory, settings, slug="intel-b"
    )

    # Tenant A ingests one restricted item (free_access → no body) and
    # one public-domain item (body allowed).
    restricted = _intel_item(
        provider="openbb",
        source_id="restricted-1",
        headline="AAPL beats estimates",
        license_class=IntelLicenseClass.FREE_ACCESS,
        body="FULL UNLICENSED ARTICLE TEXT THAT MUST NEVER BE SERVED",
    )
    public = _intel_item(
        provider="sec",
        source_id="public-1",
        headline="Apple reports earnings",
        license_class=IntelLicenseClass.PUBLIC_DOMAIN,
        body="Full public-domain filing text",
    )
    await _ingest(
        session_factory, tenant_a["tenant_id"], "openbb", [restricted]
    )
    await _ingest(session_factory, tenant_a["tenant_id"], "sec", [public])

    return {"tenant_a": tenant_a, "tenant_b": tenant_b}


# ═══════════════════════════════════════════════════════════════════════
# Source catalog (REST)
# ═══════════════════════════════════════════════════════════════════════


class TestSourceCatalogREST:
    async def test_sources_requires_auth(
        self, intel_client: httpx.AsyncClient
    ) -> None:
        """No token → 401."""
        resp = await intel_client.get("/api/v1/market-intelligence/sources")
        assert resp.status_code == 401

    async def test_sources_serves_catalog_without_secrets(
        self,
        intel_client: httpx.AsyncClient,
        seeded_read_tenant: dict[str, Any],
    ) -> None:
        """Authenticated tenant gets the catalog; no secrets, no content."""
        headers = seeded_read_tenant["tenant_a"]["headers"]
        resp = await intel_client.get(
            "/api/v1/market-intelligence/sources", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sources = {s["provider"]: s for s in body["sources"]}
        assert set(sources) == {"sec", "sec_press", "openbb"}

        # Metadata fields present.
        for source in sources.values():
            assert source["display_name"]
            assert source["license_note"]
            assert source["config_url"]
            assert source["rate_limit"]["max_requests"] > 0
            assert source["freshness"]["max_age_seconds"] > 0
            # config flags are key *names* only.
            for flag in source["config_flags"]:
                assert flag.isupper()

        # No secret-shaped fields/values in the whole payload.
        raw = resp.text.lower()
        for shape in ("bearer ", "sk-", "token=", "encrypted_payload", "nonce"):
            assert shape not in raw, f"secret leaked in catalog: {shape}"

        # No item-content fields in the catalog.
        for source in sources.values():
            assert not {"headline", "summary", "body"} & set(source.keys())


# ═══════════════════════════════════════════════════════════════════════
# Item read contract (REST)
# ═══════════════════════════════════════════════════════════════════════


class TestItemReadContractREST:
    async def test_items_tenant_scoped_and_restricted_body_never_served(
        self,
        intel_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_read_tenant: dict[str, Any],
    ) -> None:
        """Tenant A's items are readable by A; restricted items carry no
        body; tenant B cannot read tenant A's record (404)."""
        tenant_a = seeded_read_tenant["tenant_a"]
        tenant_b = seeded_read_tenant["tenant_b"]

        # A lists its own items.
        resp_a = await intel_client.get(
            "/api/v1/market-intelligence/items", headers=tenant_a["headers"]
        )
        assert resp_a.status_code == 200, resp_a.text
        items_a = resp_a.json()["items"]
        assert len(items_a) == 2

        by_source = {i["source_id"]: i for i in items_a}
        restricted = by_source["restricted-1"]
        # Restricted class → body never served (licence compliance).
        assert restricted["body"] is None
        assert "FULL UNLICENSED" not in resp_a.text
        public = by_source["public-1"]
        assert public["body"] == "Full public-domain filing text"

        # B cannot see A's items.
        resp_b = await intel_client.get(
            "/api/v1/market-intelligence/items", headers=tenant_b["headers"]
        )
        assert resp_b.status_code == 200
        assert resp_b.json()["total"] == 0

        # B cannot read A's item by id (404, existence never leaks).
        resp_cross = await intel_client.get(
            f"/api/v1/market-intelligence/items/{restricted['id']}",
            headers=tenant_b["headers"],
        )
        assert resp_cross.status_code == 404

        # A can read its own item by id.
        resp_own = await intel_client.get(
            f"/api/v1/market-intelligence/items/{restricted['id']}",
            headers=tenant_a["headers"],
        )
        assert resp_own.status_code == 200
        assert resp_own.json()["body"] is None

    async def test_providers_state_sanitised(
        self,
        intel_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_read_tenant: dict[str, Any],
        resolver: _NoopResolver,
    ) -> None:
        """Provider-state endpoint returns sanitised errors, never keys."""
        tenant_a = seeded_read_tenant["tenant_a"]

        # Simulate a failed run whose raw error echoed a credential.
        async with session_factory() as session:
            uow = UnitOfWork(session)
            service = IntelIngestionService(uow, resolver)  # type: ignore[arg-type]
            provider = SecPressReleaseProvider()
            await service.record_provider_run(
                tenant_a["tenant_id"],
                provider,
                capability=IntelCapability.NEWS,
                status="unavailable",
                error=(
                    "GET https://example.com/?api_key=sk_live_1234567890abcdef "
                    "returned 403"
                ),
            )
            await uow.commit()

        resp = await intel_client.get(
            "/api/v1/market-intelligence/providers",
            headers=tenant_a["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sec_press = next(s for s in body if s["provider"] == "sec_press")
        assert sec_press["status"] == "unavailable"
        assert sec_press["last_error"] is not None
        # The credential value must never surface, even in errors.
        assert "sk_live_1234567890abcdef" not in resp.text
        assert "sk_live" not in resp.text


# ═══════════════════════════════════════════════════════════════════════
# Source catalog (MCP)
# ═══════════════════════════════════════════════════════════════════════


class TestSourceCatalogMCP:
    async def test_mcp_catalog_tool_and_resource_registered(self) -> None:
        """list_intel_sources tool + finance://intel-sources resource exist."""
        from finance_sync.mcp.server import mcp

        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "list_intel_sources" in tool_names

        uris = {
            str(t.uri_template) for t in mcp._resource_manager.list_templates()
        }
        assert "finance://intel-sources" in uris
