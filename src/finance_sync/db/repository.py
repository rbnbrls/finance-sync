"""Generic async repository pattern for SQLAlchemy 2.0.

Provides ``Repository[T]`` — a generic base class with common CRUD
operations — and concrete subclasses for each domain model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import select
from sqlalchemy import update as sa_update

from finance_sync.db import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT", bound=Base)


class Repository[ModelT: Base]:
    """Generic repository wrapping an async SQLAlchemy session.

    Type parameter ``T`` must be a SQLAlchemy declarative model class.

    Subclasses **must** set ``model_class``::

        class AccountRepository(Repository[Account]):
            model_class = Account

    Usage::

        repo = AccountRepository(session)
        account = await repo.get(account_id)
    """

    model_class: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    # ── CRUD ─────────────────────────────────────────────────────────

    async def add(self, entity: ModelT) -> ModelT:
        """Persist a new entity and return it (populated with DB defaults)."""
        self._session.add(entity)
        await self._session.flush()
        await self._session.refresh(entity)
        return entity

    async def get(self, entity_id: Any) -> ModelT | None:
        """Retrieve an entity by primary key, or ``None``."""
        return await self._session.get(self.model_class, entity_id)

    async def list(
        self,
        *filters: Any,
        order_by: Any | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[ModelT]:
        """Return all matching entities, optionally
        filtered/sorted/paginated.
        """
        stmt = select(self.model_class)
        for f in filters:
            stmt = stmt.where(f)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update(self, entity: ModelT) -> ModelT:
        """Merge changes and return the updated entity.

        The entity should already be tracked by the session (e.g. loaded
        via ``get()``, then attributes mutated).  This method calls
        ``flush()`` to persist pending changes.
        """
        await self._session.flush()
        await self._session.refresh(entity)
        return entity

    async def update_fields(
        self,
        entity_id: Any,
        **values: Any,
    ) -> ModelT | None:
        """Update specific columns by primary key without loading the row.

        Returns the refreshed entity or ``None`` if not found.
        """
        stmt = (
            sa_update(self.model_class)
            .where(self.model_class.id == entity_id)  # type: ignore[attr-defined]
            .values(**values)
            .returning(self.model_class)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        row = result.scalar_one_or_none()
        if row is not None:
            await self._session.refresh(row)
        return row

    async def delete(self, entity: ModelT) -> None:
        """Remove an entity from the session."""
        await self._session.delete(entity)
        await self._session.flush()
