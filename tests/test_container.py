"""Tests for the dependency injection container."""

from __future__ import annotations

import pytest

from finance_sync.config.settings import Settings
from finance_sync.container import Container


class TestContainerConstruction:
    """Container.from_settings() behaviour."""

    def test_minimal_container(self) -> None:
        """Container can be built without DB/Redis."""
        settings = Settings()
        container = Container.from_settings(settings)
        assert container.settings is settings

    def test_settings_property(self) -> None:
        container = Container.from_settings(Settings())
        assert container.settings.app_name == "finance-sync"

    def test_engine_raises_without_db(self) -> None:
        container = Container.from_settings(
            Settings(_env_file=None, database_url=None)
        )
        with pytest.raises(
            RuntimeError, match="Database engine not configured"
        ):
            _ = container.engine

    def test_session_factory_raises_without_db(self) -> None:
        container = Container.from_settings(
            Settings(_env_file=None, database_url=None)
        )
        with pytest.raises(
            RuntimeError, match="Database engine not configured"
        ):
            _ = container.session_factory

    def test_redis_raises_without_redis(self) -> None:
        container = Container.from_settings(
            Settings(_env_file=None, redis_url=None)
        )
        with pytest.raises(RuntimeError, match="Redis not configured"):
            _ = container.redis_client

    def test_uninitialised_container_raises(self) -> None:
        container = Container()
        with pytest.raises(RuntimeError, match="Container not initialised"):
            _ = container.settings

    def test_dispose_minimal(self) -> None:
        """Dispose on a minimal container should not raise."""
        import anyio

        async def run() -> None:
            settings = Settings()
            container = Container.from_settings(settings)
            async with container.dispose():
                pass

        anyio.run(run)


class TestContainerWithDB:
    """Container initialised with a database URL."""

    def test_engine_created(self) -> None:
        """Engine is created when database_url is set."""
        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db"  # type: ignore[call-arg]
        )
        container = Container.from_settings(settings)
        engine = container.engine
        assert engine is not None
        assert "localhost" in str(engine.url)

    def test_session_factory_created(self) -> None:
        """Session factory is created when database_url is set."""
        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db"  # type: ignore[call-arg]
        )
        container = Container.from_settings(settings)
        factory = container.session_factory
        assert factory is not None

    def test_dispose_with_engine(self) -> None:
        """Dispose with an engine does not raise."""
        import anyio

        async def run() -> None:
            settings = Settings(
                database_url="postgresql+asyncpg://u:p@localhost:5432/db"  # type: ignore[call-arg]
            )
            container = Container.from_settings(settings)
            async with container.dispose():
                pass

        anyio.run(run)

    def test_with_redis(self) -> None:
        """Container with Redis URL creates a client."""
        settings = Settings(
            redis_url="redis://localhost:6379/0",  # type: ignore[call-arg]
        )
        container = Container.from_settings(settings)
        client = container.redis_client
        assert client is not None
