"""E2E tests — per-destination replay-safe delivery and idempotent retries.

Runs against the real PostgreSQL harness with the **real** Actual Budget
exporter, but a fake external ``ActualBudgetClient`` so the delivery-cursor
and dedup logic is exercised end-to-end without a live server.

Asserts the acceptance criteria:
* each app destination gets its own replay-safe delivery cursor (one row
  per ``(tenant, target, account)``);
* a repeated run is idempotent: no duplicate delivery rows and the external
  consumer sees each transaction only once (dedup by ``imported_id``);
* a destination that fails is safely resumable without losing/duplicating
  data in another destination.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from finance_sync.exporter.actual_budget.models import ExportDelivery
from finance_sync.models import Account, Transaction
from tests.e2e.destinations_helpers import (
    create_destination,
    dest_client,
    seeded_destination_tenant,
)

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.e2e

seeded_tenant = seeded_destination_tenant
client = dest_client

AB_SERVER_URL = "http://192.168.1.60:5006"
AB_ACCOUNT_NAME = "E2E Checking"


class FakeActualBudgetClient:
    """External AB client that dedups by ``imported_id`` (like AB itself).

    The exporter constructs a fresh client per export run — like a real
    client opening a new connection to the same AB server — so all server
    state (accounts, imported-id dedup) is class-level and persists across
    runs.  Counts how many unique transactions were imported across all
    runs, so a redelivered batch can never be double-counted.
    """

    created_accounts: dict[str, dict[str, Any]] = {}
    seen_imported_ids: set[str] = set()
    imported_count: int = 0
    fail_imports: bool = False

    def __init__(self, config: object) -> None:
        self.config = config

    async def __aenter__(self) -> FakeActualBudgetClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_accounts(self) -> list[dict[str, Any]]:
        return list(type(self).created_accounts.values())

    async def get_account_by_name(self, name: str) -> dict[str, Any] | None:
        return type(self).created_accounts.get(name)

    async def get_or_create_account(
        self, name: str, *, off_budget: bool = False
    ) -> dict[str, Any]:
        accounts = type(self).created_accounts
        existing = accounts.get(name)
        if existing is not None:
            return existing
        acct = {
            "id": f"ab-{len(accounts)}",
            "name": name,
            "offbudget": off_budget,
        }
        accounts[name] = acct
        return acct

    async def import_transactions_batch(
        self, account: str, transactions: list[dict[str, Any]]
    ) -> int:
        if type(self).fail_imports:
            _msg = "external AB server unreachable"
            raise RuntimeError(_msg)
        for txn in transactions:
            imported_id = txn.get("imported_id")
            if (
                imported_id is None
                or imported_id not in type(self).seen_imported_ids
            ):
                type(self).seen_imported_ids.add(imported_id)
                type(self).imported_count += 1
        # The adapter reports successful reconciliation, not only newly
        # inserted rows.  Existing imported IDs are idempotent successes and
        # must advance this destination's cursor as well.
        return len(transactions)


@pytest.fixture(autouse=True)
def _stub_ab_client(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> FakeActualBudgetClient:
    # Reset the shared server state so each test starts clean.
    FakeActualBudgetClient.created_accounts = {}
    FakeActualBudgetClient.seen_imported_ids = set()
    FakeActualBudgetClient.imported_count = 0
    FakeActualBudgetClient.fail_imports = False
    stub = FakeActualBudgetClient(object())
    # The wizard API lazy-imports the client module…
    monkeypatch.setattr(
        "finance_sync.exporter.actual_budget.client.ActualBudgetClient",
        FakeActualBudgetClient,
    )
    # …but the exporter bound the class reference at import time, so it
    # must be patched on the exporter module itself.
    monkeypatch.setattr(
        "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
        FakeActualBudgetClient,
    )
    return stub


async def _seed_account_with_transactions(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> str:
    """Seed one canonical account with 3 transactions; returns its id."""
    async with session_factory() as session:
        acct = Account(
            provider_key="bunq",
            external_account_id="ext_checking",
            tenant_id=tenant_id,
            name=AB_ACCOUNT_NAME,
            account_type="checking",
            currency_code="EUR",
            is_active=True,
        )
        session.add(acct)
        # Flush so ``acct.id`` is materialised (Python-side ``uuid4``
        # default runs at flush time) before transactions reference it.
        await session.flush()
        for idx in range(3):
            session.add(
                Transaction(
                    tenant_id=tenant_id,
                    account_id=acct.id,
                    amount=Decimal(f"-{idx + 1}.00"),
                    currency_code="EUR",
                    occurred_at=datetime.now(UTC),
                    booked_at=datetime.now(UTC),
                    description=f"Coffee {idx}",
                    transaction_type="payment",
                    status="booked",
                    provider_key="bunq",
                    external_transaction_id=f"e2e_tx_{idx}",
                )
            )
        await session.commit()
        return str(acct.id)


async def _delivery_rows(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> list[dict[str, Any]]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExportDelivery).where(
                        ExportDelivery.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                # asyncpg returns UUID objects for pk_uuid columns.
                "target_id": str(r.target_id),
                "account_id": str(r.account_id),
            }
            for r in rows
        ]


async def _run_and_expect_completed(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    target_id: str,
) -> None:
    resp = await client.post(
        f"/api/v1/destinations/{target_id}/run", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed", resp.text


class TestPerDestinationReplaySafeDelivery:
    """Delivery and retry are scoped and idempotent per destination."""

    async def test_repeated_run_is_idempotent_and_scoped_per_destination(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_tenant: dict[str, str],
        _stub_ab_client: FakeActualBudgetClient,
    ) -> None:
        headers = seeded_tenant["headers"]
        tenant_id = seeded_tenant["tenant_id"]
        account_id = await _seed_account_with_transactions(
            session_factory, tenant_id
        )

        # Two destinations both selecting the same canonical account.
        ids: list[str] = []
        for name in ("Retry A", "Retry B"):
            draft = await create_destination(
                client,
                headers,
                target_type="actual-budget",
                display_name=name,
                configuration={"server_url": AB_SERVER_URL},
                secret={"password": "pw"},
                selected_account_ids=[account_id],
                datasets=["accounts", "transactions"],
            )
            await client.post(
                f"/api/v1/destinations/{draft['id']}/activate", headers=headers
            )
            ids.append(draft["id"])

        # First run on both destinations.
        await _run_and_expect_completed(client, headers, ids[0])
        await _run_and_expect_completed(client, headers, ids[1])

        # Each (target, account) got exactly ONE delivery cursor row —
        # per-destination replay safety even though both share the account.
        rows = await _delivery_rows(session_factory, tenant_id)
        assert len(rows) == 2
        assert {r["target_id"] for r in rows} == set(ids)
        assert {r["account_id"] for r in rows} == {account_id}

        # A repeat run is idempotent: no new delivery rows.
        await _run_and_expect_completed(client, headers, ids[0])
        rows_after_retry = await _delivery_rows(session_factory, tenant_id)
        assert len(rows_after_retry) == 2

        # The external consumer dedups by imported_id: exactly 3 unique
        # transactions across every run, never 3xN.
        assert _stub_ab_client.imported_count == 3

    async def test_failed_run_resumable_in_other_destination(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_tenant: dict[str, str],
        _stub_ab_client: FakeActualBudgetClient,
    ) -> None:
        """A failing destination never blocks or corrupts a sibling."""
        headers = seeded_tenant["headers"]
        tenant_id = seeded_tenant["tenant_id"]
        account_id = await _seed_account_with_transactions(
            session_factory, tenant_id
        )

        draft = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="Broken",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
            selected_account_ids=[account_id],
            datasets=["accounts", "transactions"],
        )
        await client.post(
            f"/api/v1/destinations/{draft['id']}/activate", headers=headers
        )
        broken_id = draft["id"]

        # Break the external client → the first run fails cleanly.
        FakeActualBudgetClient.fail_imports = True
        resp = await client.post(
            f"/api/v1/destinations/{broken_id}/run", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        FakeActualBudgetClient.fail_imports = False

        # A healthy sibling destination still runs fine afterwards.
        healthy = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="Healthy",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
            selected_account_ids=[account_id],
            datasets=["accounts", "transactions"],
        )
        await client.post(
            f"/api/v1/destinations/{healthy['id']}/activate", headers=headers
        )
        resp = await client.post(
            f"/api/v1/destinations/{healthy['id']}/run", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

        # Only the healthy destination advanced its delivery cursor: the
        # broken run failed before delivering anything, so it has no row —
        # a retry of the broken destination starts from the same window.
        rows = await _delivery_rows(session_factory, tenant_id)
        assert [r["target_id"] for r in rows] == [healthy["id"]]
