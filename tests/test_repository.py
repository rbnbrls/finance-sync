"""Tests for the generic ``Repository[T]`` and ``UnitOfWork`` patterns.

Uses an async in-memory SQLite database to test actual persistence.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import MetaData, String
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

from finance_sync.db.repository import Repository
from finance_sync.db.uow import UnitOfWork

# ── Isolated test metadata & base — no JSONB columns ────────────────

_test_metadata = MetaData()
TestBase = declarative_base(metadata=_test_metadata)


class Widget(TestBase):
    """Simple test model with basic column types only (SQLite compat)."""

    __tablename__ = "widgets"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="generic")


class WidgetRepository(Repository[Widget]):
    model_class = Widget


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    """Create a fresh in-memory SQLite async engine per test session."""
    return create_async_engine("sqlite+aiosqlite://", echo=False)


@pytest.fixture
async def tables(engine):
    """Create Widget table before each test, drop after."""
    async with engine.begin() as conn:
        await conn.run_sync(TestBase.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(TestBase.metadata.drop_all)


@pytest.fixture
async def session_factory(engine, tables):
    """Return a session factory bound to the test engine."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    """Provide a fresh async session per test."""
    async with session_factory() as s:
        yield s


@pytest.fixture
async def repo(session):
    """Provide a widget repository."""
    return WidgetRepository(session)


# ── Helper ───────────────────────────────────────────────────────────


async def _create_widget(
    repo: WidgetRepository,
    name: str = "test-widget",
    kind: str = "generic",
) -> Widget:
    """Create a widget and return it."""
    widget = Widget(name=name, kind=kind)
    return await repo.add(widget)


# ═══════════════════════════════════════════════════════════════════════
# Repository tests
# ═══════════════════════════════════════════════════════════════════════


class TestRepositoryAdd:
    async def test_add_generates_id(self, repo: WidgetRepository) -> None:
        widget = Widget(name="my-widget")
        saved = await repo.add(widget)
        assert saved.id is not None
        assert saved.name == "my-widget"

    async def test_add_returns_refreshed(self, repo: WidgetRepository) -> None:
        """After add, the entity has DB-generated defaults."""
        widget = Widget(name="no-kind")
        saved = await repo.add(widget)
        assert saved.kind == "generic"  # DB default


class TestRepositoryGet:
    async def test_get_existing(self, repo: WidgetRepository) -> None:
        saved = await _create_widget(repo, name="find-me")
        found = await repo.get(saved.id)
        assert found is not None
        assert found.name == "find-me"

    async def test_get_missing(self, repo: WidgetRepository) -> None:
        found = await repo.get("nonexistent-id")
        assert found is None


class TestRepositoryList:
    async def test_list_all(self, repo: WidgetRepository) -> None:
        await _create_widget(repo, name="a")
        await _create_widget(repo, name="b")
        all_widgets = await repo.list()
        assert len(all_widgets) == 2

    async def test_list_with_filters(self, repo: WidgetRepository) -> None:
        await _create_widget(repo, name="alpha", kind="special")
        await _create_widget(repo, name="beta", kind="generic")

        filtered = await repo.list(Widget.kind == "special")  # type: ignore[attr-defined]
        assert len(filtered) == 1
        assert filtered[0].name == "alpha"

    async def test_list_pagination(self, repo: WidgetRepository) -> None:
        for i in range(10):
            await _create_widget(repo, name=f"w-{i}")
        page1 = await repo.list(limit=3, offset=0)
        assert len(page1) == 3
        assert page1[0].name == "w-0"
        page2 = await repo.list(limit=3, offset=3)
        assert len(page2) == 3
        assert page2[0].name == "w-3"

    async def test_list_empty(self, repo: WidgetRepository) -> None:
        result = await repo.list()
        assert result == []


class TestRepositoryUpdate:
    async def test_update_mutated_entity(self, repo: WidgetRepository) -> None:
        saved = await _create_widget(repo, name="original")
        saved.name = "updated"
        updated = await repo.update(saved)
        assert updated.name == "updated"

        # Verify persistence
        refetched = await repo.get(saved.id)
        assert refetched is not None
        assert refetched.name == "updated"


class TestRepositoryUpdateFields:
    async def test_update_fields(self, repo: WidgetRepository) -> None:
        saved = await _create_widget(repo, name="old-name")
        updated = await repo.update_fields(saved.id, name="new-name")
        assert updated is not None
        assert updated.name == "new-name"

        refetched = await repo.get(saved.id)
        assert refetched is not None
        assert refetched.name == "new-name"

    async def test_update_fields_missing(self, repo: WidgetRepository) -> None:
        result = await repo.update_fields("nonexistent", name="ghost")
        assert result is None


class TestRepositoryDelete:
    async def test_delete_entity(self, repo: WidgetRepository) -> None:
        saved = await _create_widget(repo, name="delete-me")
        await repo.delete(saved)
        found = await repo.get(saved.id)
        assert found is None

    async def test_delete_twice_does_not_raise(
        self, repo: WidgetRepository
    ) -> None:
        """Deleting an already-deleted entity is safe (no exception)."""
        saved = await _create_widget(repo, name="gone")
        await repo.delete(saved)
        # Second delete should not raise (SQLAlchemy handles gracefully)
        await repo.delete(saved)


# ═══════════════════════════════════════════════════════════════════════
# UnitOfWork tests
# ═══════════════════════════════════════════════════════════════════════


class TestUnitOfWork:
    async def test_commit_on_success(self, session_factory) -> None:
        """UoW commits when the block succeeds."""
        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            repo = WidgetRepository(uow.session)
            await repo.add(Widget(name="uow-widget"))

        # Verify in a new session
        async with session_factory() as check_session:
            result = await check_session.execute(
                Widget.__table__.select()  # type: ignore[attr-defined]
            )
            rows = result.fetchall()
            assert len(rows) == 1

    async def test_rollback_on_error(self, session_factory) -> None:
        """UoW rolls back when an exception occurs."""
        async with session_factory() as session:
            try:
                async with UnitOfWork(session) as uow:
                    repo = WidgetRepository(uow.session)
                    await repo.add(Widget(name="rollback-me"))
                    msg = "Something went wrong"
                    raise RuntimeError(msg)
            except RuntimeError:
                pass

        # Verify nothing was persisted
        async with session_factory() as check_session:
            result = await check_session.execute(
                Widget.__table__.select()  # type: ignore[attr-defined]
            )
            rows = result.fetchall()
            assert len(rows) == 0

    async def test_explicit_commit(self, session_factory) -> None:
        """Explicit commit() persists within the UoW context."""
        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            repo = WidgetRepository(uow.session)
            await repo.add(Widget(name="explicit"))
            await uow.commit()

        async with session_factory() as check_session:
            result = await check_session.execute(
                Widget.__table__.select()  # type: ignore[attr-defined]
            )
            rows = result.fetchall()
            assert len(rows) == 1

    async def test_explicit_rollback(self, session_factory) -> None:
        """Explicit rollback() discards changes within the UoW context."""
        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            repo = WidgetRepository(uow.session)
            await repo.add(Widget(name="rolled"))
            await uow.rollback()

        async with session_factory() as check_session:
            result = await check_session.execute(
                Widget.__table__.select()  # type: ignore[attr-defined]
            )
            rows = result.fetchall()
            assert len(rows) == 0

    async def test_repo_caching(self, session_factory) -> None:
        """UoW lazily creates and caches repository instances."""
        async with session_factory() as session:
            uow = UnitOfWork(session)
            r1 = uow._repo("widgets", WidgetRepository)
            r2 = uow._repo("widgets", WidgetRepository)
            assert r1 is r2  # same instance
