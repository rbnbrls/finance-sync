"""Tests for the security-identity + encrypted-credential layer.

Covers the acceptance criteria of task t_82482699:

* observations link to a security **only** when identity is unambiguous;
* ambiguous matches land in the review queue, never silently linked;
* provider credentials are envelope-encrypted (AES-256-GCM), never
  stored or returned as plaintext;
* errors/logs/API surfaces never leak secrets (redaction proof);
* manual review-queue resolution links the observation safely.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
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

from finance_sync.config.settings import Settings
from finance_sync.db import Base
from finance_sync.db.uow import UnitOfWork
from finance_sync.enrichment.models import ResolvedSecurity
from finance_sync.intel.adapters.openbb import OpenBBIntelProvider
from finance_sync.intel.credentials import (
    INTEL_CREDENTIAL_PREFIX,
    IntelCredentialStore,
)
from finance_sync.intel.enums import (
    IntelItemKind,
    IntelLicenseClass,
    IntelResolutionStatus,
)
from finance_sync.intel.hashing import content_hash
from finance_sync.intel.models import IntelItem
from finance_sync.intel.service import IntelIngestionService
from finance_sync.models.credential import Credential
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.market_intelligence_review_queue import (
    MarketIntelligenceReviewQueue,
)
from finance_sync.models.security import Security
from finance_sync.services.market_intelligence_review import (
    IntelReviewService,
    ReviewQueueError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

#: Fake security ids used as resolution targets.  The identity tests
#: create real ``Security`` rows so FK constraints (``securities.id``)
#: hold under SQLite the same way they do on PostgreSQL.  The ids
#: contain hex letters (a/b): SQLite's NUMERIC affinity would coerce
#: an all-digit UUID hex into an INTEGER, breaking the round-trip.
FAKE_SEC_A = "aaaa1111-aaaa-4aaa-8aaa-aaaa11111111"
FAKE_SEC_B = "bbbb2222-bbbb-4bbb-8bbb-bbbb22222222"


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
    return Settings(
        secret_key=SecretStr("test-secret-that-is-long-enough"),
        master_encryption_key=SecretStr("a" * 64),  # 32 hex bytes = 32 bytes
        database_url=None,
        redis_url=None,
    )


def _item(
    *,
    provider: str = "openbb",
    source_id: str | None = None,
    headline: str = "AAPL beats estimates",
    identifiers: dict[str, str] | None = None,
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
        summary="Short summary",
        store_full_text=False,
        store_summary=True,
        identifiers=identifiers or {},
    )


class _FakeResolver:
    """Minimal SecurityResolver double with a configurable match table."""

    def __init__(self, matches: dict[tuple[str, str], Any]) -> None:
        self.matches = matches

    async def resolve_by_isin(self, isin: str) -> Any:
        return self.matches.get(("isin", isin))

    async def resolve_by_figi(self, figi: str) -> Any:
        return self.matches.get(("figi", figi))

    async def resolve_by_ticker(self, ticker: str) -> Any:
        return self.matches.get(("ticker", ticker))


def _resolved(
    security_id: str | None = None, confidence: str = "exact"
) -> ResolvedSecurity:
    return ResolvedSecurity(
        security_id=security_id or str(uuid4()),
        isin="US0378331005",
        figi=None,
        ticker="AAPL",
        name="Apple Inc.",
        currency_code="USD",
        confidence=confidence,
        source="local_db",
    )


async def _create_security(
    session_factory: async_sessionmaker,
    security_id: str,
    *,
    isin: str = "US0378331005",
    ticker: str = "AAPL",
    name: str = "Apple Inc.",
) -> None:
    """Insert a canonical Security row (FK target for resolutions)."""
    async with session_factory() as session:
        session.add(
            Security(
                id=security_id,
                isin=isin,
                ticker=ticker,
                name=name,
                security_type="stock",
                currency_code="USD",
            )
        )
        await session.commit()


async def _create_two_securities(
    session_factory: async_sessionmaker,
) -> tuple[str, str]:
    """Create two distinct canonical securities (different ISINs)."""
    await _create_security(
        session_factory,
        FAKE_SEC_A,
        isin="US0378331005",
        ticker="AAPL",
        name="Apple Inc.",
    )
    await _create_security(
        session_factory,
        FAKE_SEC_B,
        isin="US5949181045",
        ticker="MSFT",
        name="Microsoft Corp.",
    )
    return FAKE_SEC_A, FAKE_SEC_B


@pytest.fixture
def fake_resolver() -> _FakeResolver:
    return _FakeResolver({})


async def _ingest(
    session_factory: async_sessionmaker,
    resolver: Any,
    tenant_id: str,
    provider: str,
    items: list[IntelItem],
) -> dict[str, int]:
    async with session_factory() as session:
        uow = UnitOfWork(session)
        service = IntelIngestionService(uow, resolver)
        result = await service.ingest_items(tenant_id, provider, items)
        await uow.commit()
    return result


async def _query_one(
    session_factory: async_sessionmaker, model: Any, **filters: Any
) -> Any:
    async with session_factory() as session:
        stmt = select(model)
        for col, val in filters.items():
            stmt = stmt.where(getattr(model, col) == val)
        rows = (await session.execute(stmt)).scalars().all()
        rows = list(rows)
        return rows[0] if rows else None


# ═══════════════════════════════════════════════════════════════════════
# Identity resolution — unambiguous links only
# ═══════════════════════════════════════════════════════════════════════


class TestIdentityResolution:
    async def test_exact_isin_links_unambiguously(
        self, session_factory: async_sessionmaker
    ) -> None:
        """An exact ISIN match links the observation to the security."""
        tenant_id = str(uuid4())
        await _create_security(session_factory, FAKE_SEC_A)
        resolver = _FakeResolver(
            {("isin", "US0378331005"): _resolved(FAKE_SEC_A, "exact")}
        )
        item = _item(
            source_id="res-1",
            identifiers={"isin": "US0378331005"},
        )

        result = await _ingest(
            session_factory, resolver, tenant_id, "openbb", [item]
        )
        assert result["ingested"] == 1
        assert result["review_required"] == 0

        row = await _query_one(
            session_factory,
            MarketIntelligenceItem,
            tenant_id=tenant_id,
        )
        assert row is not None
        assert str(row.security_id) == FAKE_SEC_A
        assert row.resolution_status == IntelResolutionStatus.RESOLVED.value
        assert row.review_required is False

    async def test_low_confidence_match_goes_to_review(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A bare ticker (ticker_only confidence) is never auto-linked."""
        tenant_id = str(uuid4())
        await _create_security(session_factory, FAKE_SEC_A)
        resolver = _FakeResolver(
            {("ticker", "NOK"): _resolved(FAKE_SEC_A, "ticker_only")}
        )
        item = _item(
            source_id="res-2",
            headline="NOK surges after results",
            identifiers={"ticker": "NOK"},
        )

        result = await _ingest(
            session_factory, resolver, tenant_id, "openbb", [item]
        )
        assert result["review_required"] == 1

        row = await _query_one(
            session_factory,
            MarketIntelligenceItem,
            tenant_id=tenant_id,
        )
        assert row is not None
        assert row.security_id is None
        assert row.resolution_status == IntelResolutionStatus.AMBIGUOUS.value
        assert row.review_required is True

        entry = await _query_one(
            session_factory,
            MarketIntelligenceReviewQueue,
            tenant_id=tenant_id,
        )
        assert entry is not None
        assert entry.resolution_status == "pending"
        assert entry.candidate_identifiers is not None
        assert entry.candidate_identifiers[0]["security_id"] == FAKE_SEC_A

    async def test_ambiguous_multiple_ids_never_linked(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Identifiers resolving to *different* securities → review queue."""
        tenant_id = str(uuid4())
        await _create_two_securities(session_factory)
        resolver = _FakeResolver(
            {
                ("ticker", "NOK"): _resolved(FAKE_SEC_A, "ticker_only"),
                ("figi", "BBG000XD5XL7"): _resolved(FAKE_SEC_B, "exact"),
            }
        )
        item = _item(
            source_id="res-3",
            headline="NOK surges",
            identifiers={"ticker": "NOK", "figi": "BBG000XD5XL7"},
        )

        result = await _ingest(
            session_factory, resolver, tenant_id, "openbb", [item]
        )
        assert result["review_required"] == 1

        row = await _query_one(
            session_factory,
            MarketIntelligenceItem,
            tenant_id=tenant_id,
        )
        assert row is not None
        assert row.security_id is None
        assert row.resolution_status == IntelResolutionStatus.AMBIGUOUS.value

        entry = await _query_one(
            session_factory,
            MarketIntelligenceReviewQueue,
            tenant_id=tenant_id,
        )
        assert entry is not None
        assert len(entry.candidate_identifiers or []) == 2

    async def test_no_match_stays_unresolved(
        self, session_factory: async_sessionmaker, fake_resolver: Any
    ) -> None:
        """No matching security → item stays unresolved, no review entry."""
        tenant_id = str(uuid4())
        item = _item(
            source_id="res-4",
            identifiers={"ticker": "ZZZZ"},
        )

        result = await _ingest(
            session_factory, fake_resolver, tenant_id, "openbb", [item]
        )
        assert result["ingested"] == 1
        assert result["review_required"] == 0

        row = await _query_one(
            session_factory,
            MarketIntelligenceItem,
            tenant_id=tenant_id,
        )
        assert row is not None
        assert row.security_id is None
        assert row.resolution_status == IntelResolutionStatus.UNRESOLVED.value


# ═══════════════════════════════════════════════════════════════════════
# Review queue — manual resolution
# ═══════════════════════════════════════════════════════════════════════


class TestReviewQueueResolution:
    async def test_manual_resolution_links_observation(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Resolving a queue entry links the observation to the security."""
        tenant_id = str(uuid4())
        await _create_two_securities(session_factory)
        resolver = _FakeResolver(
            {
                ("ticker", "NOK"): _resolved(FAKE_SEC_A, "ticker_only"),
                ("figi", "BBG000XD5XL7"): _resolved(FAKE_SEC_B, "exact"),
            }
        )
        item = _item(
            source_id="res-5",
            identifiers={"ticker": "NOK", "figi": "BBG000XD5XL7"},
        )
        await _ingest(session_factory, resolver, tenant_id, "openbb", [item])

        entry = await _query_one(
            session_factory,
            MarketIntelligenceReviewQueue,
            tenant_id=tenant_id,
        )
        assert entry is not None
        item_row = await _query_one(
            session_factory,
            MarketIntelligenceItem,
            tenant_id=tenant_id,
        )
        assert item_row is not None

        async with session_factory() as session:
            service = IntelReviewService(session)
            resolved = await service.resolve_entry(
                tenant_id, str(entry.id), FAKE_SEC_B, note="Confirmed via FIGI"
            )
            assert resolved is not None
            assert resolved.resolution_status == "resolved"
            assert str(resolved.resolved_security_id) == FAKE_SEC_B
            await session.commit()

        updated = await _query_one(
            session_factory,
            MarketIntelligenceItem,
            tenant_id=tenant_id,
        )
        assert updated is not None
        assert str(updated.security_id) == FAKE_SEC_B
        assert updated.resolution_status == IntelResolutionStatus.RESOLVED.value
        assert updated.review_required is False

    async def test_resolve_unknown_security_raises(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Resolving to a non-existent security raises ReviewQueueError."""
        tenant_id = str(uuid4())
        await _create_security(session_factory, FAKE_SEC_A)
        resolver = _FakeResolver(
            {("ticker", "NOK"): _resolved(FAKE_SEC_A, "ticker_only")}
        )
        item = _item(
            source_id="res-6",
            identifiers={"ticker": "NOK"},
        )
        await _ingest(session_factory, resolver, tenant_id, "openbb", [item])

        entry = await _query_one(
            session_factory,
            MarketIntelligenceReviewQueue,
            tenant_id=tenant_id,
        )
        assert entry is not None

        async with session_factory() as session:
            service = IntelReviewService(session)
            with pytest.raises(ReviewQueueError):
                await service.resolve_entry(
                    tenant_id, str(entry.id), str(uuid4())
                )

    async def test_dismiss_keeps_unlinked(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Dismissing an entry keeps the observation unlinked but unflagged."""
        tenant_id = str(uuid4())
        await _create_security(session_factory, FAKE_SEC_A)
        resolver = _FakeResolver(
            {("ticker", "NOK"): _resolved(FAKE_SEC_A, "ticker_only")}
        )
        item = _item(
            source_id="res-7",
            identifiers={"ticker": "NOK"},
        )
        await _ingest(session_factory, resolver, tenant_id, "openbb", [item])

        entry = await _query_one(
            session_factory,
            MarketIntelligenceReviewQueue,
            tenant_id=tenant_id,
        )
        assert entry is not None

        async with session_factory() as session:
            service = IntelReviewService(session)
            dismissed = await service.dismiss_entry(
                tenant_id, str(entry.id), note="False positive"
            )
            assert dismissed is not None
            assert dismissed.resolution_status == "dismissed"
            await session.commit()

        updated = await _query_one(
            session_factory,
            MarketIntelligenceItem,
            tenant_id=tenant_id,
        )
        assert updated is not None
        assert updated.security_id is None
        assert updated.review_required is False

    async def test_cross_tenant_resolve_is_404(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Another tenant's entry cannot be resolved (None result)."""
        tenant_a = str(uuid4())
        tenant_b = str(uuid4())
        await _create_security(session_factory, FAKE_SEC_A)
        resolver = _FakeResolver(
            {("ticker", "NOK"): _resolved(FAKE_SEC_A, "ticker_only")}
        )
        item = _item(
            source_id="res-8",
            identifiers={"ticker": "NOK"},
        )
        await _ingest(session_factory, resolver, tenant_a, "openbb", [item])

        entry = await _query_one(
            session_factory,
            MarketIntelligenceReviewQueue,
            tenant_id=tenant_a,
        )
        assert entry is not None

        async with session_factory() as session:
            service = IntelReviewService(session)
            result = await service.resolve_entry(
                tenant_b, str(entry.id), FAKE_SEC_A
            )
            assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Encrypted credentials
# ═══════════════════════════════════════════════════════════════════════


class TestIntelCredentialStore:
    async def test_save_encrypts_payload(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        """Stored payload is ciphertext, never the plaintext key."""
        tenant_id = str(uuid4())
        api_key = "openbb-test-key-0123456789abcdef"

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            row = await store.save(tenant_id, "openbb", {"api_key": api_key})
            await session.commit()
            assert row.encrypted_payload != api_key.encode()
            assert row.nonce is not None and len(row.nonce) == 12
            assert api_key not in (row.description or "")

        # The raw DB row never contains the plaintext.
        async with session_factory() as session:
            stmt = select(Credential).where(Credential.tenant_id == tenant_id)
            rows = (await session.execute(stmt)).scalars().all()
            assert len(list(rows)) == 1
            row = next(iter(rows))
            assert row.provider_key == f"{INTEL_CREDENTIAL_PREFIX}openbb"
            assert api_key.encode() not in row.encrypted_payload
            assert api_key not in (row.description or "")

    async def test_get_returns_decrypted(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        """get() returns the decrypted credentials (plaintext in memory only)."""
        tenant_id = str(uuid4())
        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            await store.save(tenant_id, "openbb", {"api_key": "abc123"})
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            creds = await store.get(tenant_id, "openbb")
            assert creds == {"api_key": "abc123"}

    async def test_partial_update_merges(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        """Updating one key preserves the others (merge semantics)."""
        tenant_id = str(uuid4())
        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            await store.save(
                tenant_id, "openbb", {"api_key": "key1", "token": "tok1"}
            )
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            await store.save(tenant_id, "openbb", {"api_key": "key2"})
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            creds = await store.get(tenant_id, "openbb")
            assert creds == {"api_key": "key2", "token": "tok1"}

    async def test_status_never_exposes_values(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        """status() reports key names only, never secret values."""
        tenant_id = str(uuid4())
        api_key = "super-secret-value-12345"
        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            await store.save(tenant_id, "openbb", {"api_key": api_key})
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            status_data = await store.status(tenant_id, "openbb")
            assert status_data["is_configured"] is True
            assert status_data["credential_keys"] == ["api_key"]
            assert api_key not in str(status_data)

    async def test_delete_removes_row(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        """delete() removes the credential row."""
        tenant_id = str(uuid4())
        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            await store.save(tenant_id, "openbb", {"api_key": "k"})
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            deleted = await store.delete(tenant_id, "openbb")
            assert deleted is True
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            assert await store.has(tenant_id, "openbb") is False

    async def test_record_error_sanitises_secret(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        """record_error persists a sanitised error, never the secret."""
        tenant_id = str(uuid4())
        api_key = "openbb-test-key-0123456789abcdef"
        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            await store.save(tenant_id, "openbb", {"api_key": api_key})
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            await store.record_error(
                tenant_id,
                "openbb",
                f"OpenBB 401 Unauthorized (key={api_key})",
            )
            await session.commit()

        async with session_factory() as session:
            store = IntelCredentialStore(session, settings)
            status_data = await store.status(tenant_id, "openbb")
            last_error = status_data["last_error"] or ""
            assert api_key not in last_error
            assert "401" in last_error


# ═══════════════════════════════════════════════════════════════════════
# Secret redaction (holdout H2 proof)
# ═══════════════════════════════════════════════════════════════════════


class TestSecretRedaction:
    def test_redact_text_removes_known_shapes(self) -> None:
        from finance_sync.utils.redaction import redact_text

        secret = "pk_tes...bcdef"
        cleaned = redact_text(f"Authorization: Bearer {secret}")
        assert secret not in cleaned
        assert "[REDACTED]" in cleaned

    def test_sanitize_error_truncates_and_redacts(self) -> None:
        from finance_sync.utils.redaction import sanitize_error

        api_key = "pk_tes...cdef0123456789abcdef0123456789abcdef"
        error = f"GET https://api.example.com?key={api_key} failed: 401"
        cleaned = sanitize_error(error, [api_key])
        assert api_key not in cleaned
        assert "401" in cleaned
        assert len(cleaned) <= 500


# ═══════════════════════════════════════════════════════════════════════
# OpenBB adapter: per-tenant credential injection
# ═══════════════════════════════════════════════════════════════════════


class TestOpenBBConfigure:
    def test_configure_injects_api_key(self) -> None:
        provider = OpenBBIntelProvider(api_key=None)
        assert provider.capabilities.__name__  # sanity: module importable

        provider.configure({"api_key": "pk_tes...bcdef"})
        assert provider._api_key == "pk_tes...bcdef"

    def test_configure_empty_is_noop(self) -> None:
        provider = OpenBBIntelProvider(api_key="original")
        provider.configure({})
        assert provider._api_key == "original"

    def test_configure_token_alias(self) -> None:
        provider = OpenBBIntelProvider(api_key=None)
        provider.configure({"token": "tok-123"})
        assert provider._api_key == "tok-123"


# ═══════════════════════════════════════════════════════════════════════
# OpenAPI registration — new surfaces are reachable
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAPIRegistration:
    """The identity/credentials endpoints appear in the OpenAPI contract."""

    def test_intel_surfaces_registered(self) -> None:
        from fastapi.testclient import TestClient

        from finance_sync.app import create_app
        from finance_sync.config.settings import Settings as _Settings

        settings = _Settings(
            secret_key=SecretStr("test-secret-key-16chars"),
            master_encryption_key=SecretStr("a" * 64),
            database_url=None,
            redis_url=None,
        )
        app = create_app(settings=settings)
        with TestClient(app) as client:
            paths = client.get("/openapi.json").json()["paths"]

        for path in (
            "/api/v1/market-intelligence/credentials/{provider_key}",
            "/api/v1/market-intelligence/review-queue/{entry_id}/resolve",
            "/api/v1/market-intelligence/review-queue/{entry_id}/dismiss",
        ):
            assert path in paths, f"missing OpenAPI path {path}"

        cred = paths["/api/v1/market-intelligence/credentials/{provider_key}"]
        assert set(cred) == {"get", "put", "delete"}
        review = paths[
            "/api/v1/market-intelligence/review-queue/{entry_id}/resolve"
        ]
        assert "post" in review
