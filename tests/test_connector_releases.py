"""Tests for gated connector release promotion and rollback."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finance_sync.db import Base
from finance_sync.models.connector_release import ConnectorRelease
from finance_sync.services.connector_releases import (
    ConnectorReleaseError,
    promote,
    register_candidate,
    rollback,
)


@pytest.fixture
async def release_session() -> AsyncIterator[object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(  # type: ignore[arg-type]
                sync, tables=[ConnectorRelease.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_uncertified_candidate_cannot_be_promoted(
    release_session: object,
) -> None:
    await register_candidate(
        release_session,
        provider_key="demo",
        version="1.1.0",
        previous_version="1.0.0",
        certification_status="pending",
        certification_commit=None,
        compatibility_status="compatible",
        canary_status="passed",
        capabilities=["accounts"],
    )
    with pytest.raises(ConnectorReleaseError, match="certification_required"):
        await promote(release_session, "demo", "1.1.0")


@pytest.mark.asyncio
async def test_candidate_with_failed_canary_cannot_be_promoted(
    release_session: object,
) -> None:
    await register_candidate(
        release_session,
        provider_key="demo",
        version="1.2.0",
        previous_version="1.0.0",
        certification_status="certified",
        certification_commit="abc",
        compatibility_status="compatible",
        canary_status="failed",
        capabilities=["accounts"],
    )
    with pytest.raises(ConnectorReleaseError, match="canary_required"):
        await promote(release_session, "demo", "1.2.0")


@pytest.mark.asyncio
async def test_promotion_is_idempotent_and_rollback_keeps_previous(
    release_session: object,
) -> None:
    previous = await register_candidate(
        release_session,
        provider_key="demo",
        version="1.0.0",
        previous_version=None,
        certification_status="certified",
        certification_commit="abc",
        compatibility_status="compatible",
        canary_status="passed",
        capabilities=["accounts"],
    )
    await promote(release_session, "demo", previous.version)
    candidate = await register_candidate(
        release_session,
        provider_key="demo",
        version="1.1.0",
        previous_version="1.0.0",
        certification_status="certified",
        certification_commit="def",
        compatibility_status="compatible",
        canary_status="passed",
        capabilities=["accounts"],
    )
    promoted = await promote(release_session, "demo", candidate.version)
    again = await promote(release_session, "demo", candidate.version)
    assert promoted.id == again.id
    restored = await rollback(release_session, "demo")
    assert restored.version == "1.0.0"
    assert restored.status == "enabled"
    assert restored.enabled_at is not None
