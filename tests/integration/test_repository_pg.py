"""Repository + UnitOfWork integration tests against real PostgreSQL.

These mirror ``tests/test_repository.py`` (which runs on aiosqlite) but
exercise the **real** ORM models against a migrated PostgreSQL database:
UUID primary keys, JSONB columns, FK constraints and unique constraints
are all PostgreSQL-specific behaviour the SQLite unit suite cannot cover.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from finance_sync.db.uow import UnitOfWork
from finance_sync.models import Account, Tenant
from finance_sync.models.enums import AccountType

pytestmark = pytest.mark.integration


async def _create_tenant(
    session_factory,
    *,
    slug: str = "acme",
    name: str = "ACME Corp",
) -> Tenant:
    """Create a tenant and return the persisted entity."""
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.tenants.add(Tenant(slug=slug, name=name))


async def _create_account(
    session_factory,
    tenant: Tenant,
    *,
    external_id: str = "acc_ext_1",
    provider_key: str = "mock_provider",
    name: str = "Main Checking",
    connection_id: str | None = None,
    metadata_: dict | None = None,
) -> Account:
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.accounts.add(
            Account(
                tenant_id=tenant.id,
                provider_key=provider_key,
                connection_id=connection_id,
                external_account_id=external_id,
                name=name,
                account_type=AccountType.CHECKING,
                currency_code="EUR",
                current_balance=Decimal("1520.45"),
                available_balance=Decimal("1480.00"),
                provider_metadata=metadata_
                or {"iban": "NL00BANK0123456789", "bic": "BANKNL2A"},
            )
        )


class TestRepositoryAddPg:
    """Real-model add() against PostgreSQL."""

    async def test_add_generates_uuid(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory)
        assert tenant.id is not None
        assert len(str(tenant.id)) == 36  # UUID4 string length

    async def test_add_persists_jsonb(self, session_factory) -> None:
        """JSONB provider_metadata round-trips through real PostgreSQL."""
        tenant = await _create_tenant(session_factory)
        metadata = {"iban": "NL00BANK0123456789", "bic": "BANKNL2A"}
        account = await _create_account(
            session_factory, tenant, metadata_=metadata
        )

        # Reload from a fresh session — JSONB must come back as a dict.
        async with session_factory() as session:
            reloaded = await session.get(Account, account.id)
        assert reloaded is not None
        assert reloaded.provider_metadata == metadata
        assert reloaded.tenant_id == tenant.id


class TestRepositoryGetPg:
    async def test_get_existing_and_missing(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory)
        account = await _create_account(session_factory, tenant)

        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            found = await uow.accounts.get(account.id)
            missing = await uow.accounts.get(
                "00000000-0000-0000-0000-000000000000"
            )
        assert found is not None
        assert found.name == "Main Checking"
        assert missing is None


class TestRepositoryListPg:
    async def test_list_with_filters_and_order(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory)
        await _create_account(
            session_factory, tenant, external_id="acc_a", name="Alpha"
        )
        await _create_account(
            session_factory, tenant, external_id="acc_b", name="Beta"
        )

        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            from finance_sync.models import Account as AccountModel

            all_accounts = await uow.accounts.list(
                AccountModel.tenant_id == tenant.id,  # type: ignore[attr-defined]
                order_by=AccountModel.name,  # type: ignore[attr-defined]
            )
            filtered = await uow.accounts.list(
                AccountModel.name == "Alpha",  # type: ignore[attr-defined]
            )
        assert len(all_accounts) == 2
        assert [a.name for a in all_accounts] == ["Alpha", "Beta"]
        assert len(filtered) == 1
        assert filtered[0].external_account_id == "acc_a"


class TestRepositoryUpdatePg:
    async def test_update_and_update_fields(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory)
        account = await _create_account(session_factory, tenant)

        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            # Reload in THIS session before mutating (SQLAlchemy requires
            # the instance to be persistent in the session being flushed).
            loaded = await uow.accounts.get(account.id)
            assert loaded is not None
            loaded.name = "Renamed"
            await uow.accounts.update(loaded)
            updated = await uow.accounts.update_fields(
                account.id, current_balance=Decimal("999.00")
            )
        assert updated is not None
        assert updated.current_balance == Decimal("999.00")

        async with session_factory() as session:
            reloaded = await session.get(Account, account.id)
        assert reloaded is not None
        assert reloaded.name == "Renamed"
        assert reloaded.current_balance == Decimal("999.00")


class TestRepositoryDeletePg:
    async def test_delete_entity(self, session_factory) -> None:
        tenant = await _create_tenant(session_factory)
        account = await _create_account(session_factory, tenant)

        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            await uow.accounts.delete(account)

        async with session_factory() as session:
            gone = await session.get(Account, account.id)
        assert gone is None


class TestUnitOfWorkPg:
    """UoW commit/rollback semantics on real PostgreSQL."""

    async def test_commit_on_success(self, session_factory) -> None:
        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            await uow.tenants.add(Tenant(slug="commit-me", name="Commit"))

        async with session_factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(Tenant))
            assert len(result.scalars().all()) == 1

    async def test_rollback_on_error(self, session_factory) -> None:
        async with session_factory() as session:
            try:
                async with UnitOfWork(session) as uow:
                    await uow.tenants.add(Tenant(slug="rollback-me", name="RB"))
                    msg = "boom"
                    raise RuntimeError(msg)
            except RuntimeError:
                pass

        async with session_factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(Tenant))
            assert result.scalars().all() == []

    async def test_explicit_commit_within_block(self, session_factory) -> None:
        async with (
            session_factory() as session,
            UnitOfWork(session) as uow,
        ):
            await uow.tenants.add(Tenant(slug="explicit", name="Exp"))
            await uow.commit()

        async with session_factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(Tenant))
            assert len(result.scalars().all()) == 1


class TestPgConstraints:
    """PostgreSQL-specific constraint behaviour."""

    async def test_unique_provider_constraint(self, session_factory) -> None:
        """Two accounts with the same (tenant, provider, connection,
        external) id are rejected by ``uq_accounts_provider``.

        Since migration 0017 the unique constraint is scoped per
        connection: two rows with identical external ids from *different*
        connections coexist (NULL connection_id rows are legacy and PG
        treats NULLs as distinct, so they do not collide either).
        """
        tenant = await _create_tenant(session_factory)
        await _create_account(
            session_factory, tenant, external_id="dup-1", connection_id="conn-1"
        )

        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                async with UnitOfWork(session) as uow:
                    await uow.accounts.add(
                        Account(
                            tenant_id=tenant.id,
                            provider_key="mock_provider",
                            connection_id="conn-1",
                            external_account_id="dup-1",
                            name="Duplicate",
                            account_type=AccountType.CHECKING,
                        )
                    )
                    await uow.commit()

        # A different connection with the *same* external id is allowed —
        # that is the whole point of the multi-connection feature.
        await _create_account(
            session_factory,
            tenant,
            external_id="dup-1",
            connection_id="conn-2",
        )

    async def test_fk_violation(self, session_factory) -> None:
        """Account referencing a non-existent tenant violates the FK."""
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                async with UnitOfWork(session) as uow:
                    await uow.accounts.add(
                        Account(
                            tenant_id="00000000-0000-0000-0000-000000000000",
                            provider_key="mock_provider",
                            external_account_id="orphan",
                            name="Orphan",
                            account_type=AccountType.CHECKING,
                        )
                    )
                    await uow.commit()

    async def test_unique_tenant_slug(self, session_factory) -> None:
        await _create_tenant(session_factory, slug="same-slug")

        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                async with UnitOfWork(session) as uow:
                    await uow.tenants.add(Tenant(slug="same-slug", name="Dup"))
                    await uow.commit()
