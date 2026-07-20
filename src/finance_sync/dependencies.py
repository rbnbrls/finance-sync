"""FastAPI dependency providers.

Route handlers acquire infrastructure objects through these helpers, which
read from ``request.state``.

NOTE: ``from __future__ import annotations`` is intentionally omitted here
because FastAPI needs to introspect the function signatures at runtime.
Type hints are resolved eagerly.
"""

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.config.settings import Settings
from finance_sync.container import Container


def get_container(request: Request) -> Container:
    """Return the application container stored on app state."""
    container: Container = request.app.state.container
    return container


def get_settings(request: Request) -> Settings:
    """Return the application settings from the container."""
    return get_container(request).settings


async def get_db(request: Request) -> AsyncGenerator[AsyncSession]:
    """Yield a database session, rolling back on error."""
    container = get_container(request)
    async with container.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
