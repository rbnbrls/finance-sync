"""E2E tests — destination wizard lifecycle and multi-destination isolation.

Runs against the real PostgreSQL harness.  The external agent clients
(Wealthfolio, Actual Budget) are stubbed out so the tests exercise only the
wizard API + scheduler + delivery wiring, never the live servers.

Covers the acceptance criteria around the wizard experience and the
single-owner, datalake-first guarantees:
* an empty install has a fully working datalake with zero destinations;
* create draft → test connection → discovery → preview → activate → run →
  pause → run-blocked → delete round trip;
* removing/pausing a destination never touches canonical data;
* several destinations of the same type are supported simultaneously and
  run independently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from finance_sync.models import Account, Transaction
from finance_sync.models.export_target import (
    TARGET_ACTIVE,
    TARGET_DRAFT,
    TARGET_PAUSED,
)
from tests.e2e.destinations_helpers import (
    create_destination,
    dest_client,
    seeded_destination_tenant,
)

# Alias fixtures so the tests read naturally as ``client`` / ``seeded_tenant``.
seeded_tenant = seeded_destination_tenant
client = dest_client

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.e2e

# Private self-hosted endpoint the URL/TLS validator accepts over HTTP.
AB_SERVER_URL = "http://192.168.1.50:5006"


# ── Stubbed external clients ──────────────────────────────────────────


class StubWealthfolioClient:
    """Minimal WealthfolioClient stand-in (used by /test and run_export)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def check_auth_status(self) -> None:
        return None

    async def authenticate(self) -> None:
        return None


class StubWealthfolioExporter:
    """Stand-in for WealthfolioExporter — reports a successful push."""

    async def push_to_wealthfolio(self, **kwargs: object) -> dict[str, Any]:
        return {"imported": 2, "failed": 0, "errors": []}


class StubActualBudgetClient:
    """Stand-in ActualBudgetClient — exposes one shared off-budget account
    so the mapping preview resolves an existing AB account."""

    DISCOVERED = [
        {
            "id": "ab-budget-1",
            "sync_id": "sync-1",
            "name": "E2E Budget",
            "encrypted": True,
        },
        {
            "id": "ab-budget-2",
            "sync_id": "sync-2",
            "name": "Home Budget",
            "encrypted": False,
        },
    ]

    def __init__(self, config: object) -> None:
        self.accounts = [
            {"id": "ab-checking", "name": "E2E Checking", "offbudget": True}
        ]

    @classmethod
    async def discover_budgets(cls, config: object) -> list[dict[str, Any]]:
        return list(cls.DISCOVERED)

    async def __aenter__(self) -> StubActualBudgetClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_accounts(self) -> list[dict[str, Any]]:
        return self.accounts


class StubActualBudgetExporter:
    """Stand-in for ActualBudgetExporter — reports a successful run.

    ``schedule_runner`` instantiates the real exporter with
    ``session_factory`` / ``ab_config`` / ``tenant_id`` / ``target_id``
    kwargs, so the stub must accept (and ignore) them.
    """

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs

    async def run_export(self, **kwargs: object) -> Any:
        return type(
            "ExportResult", (), {"status": "completed", "error_message": None}
        )()


@pytest.fixture(autouse=True)
def _stub_external_clients(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace Wealthfolio/Actual Budget client+exporter classes with
    stubs so no live server is contacted during the wizard E2E."""
    monkeypatch.setattr(
        "finance_sync.exporter.wealthfolio.client.WealthfolioClient",
        StubWealthfolioClient,
    )
    monkeypatch.setattr(
        "finance_sync.exporter.actual_budget.client.ActualBudgetClient",
        StubActualBudgetClient,
    )
    # The exporter bound the client reference at import time, so the
    # module-level name must be patched too for run/export flows.
    monkeypatch.setattr(
        "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
        StubActualBudgetClient,
    )
    monkeypatch.setattr(
        "finance_sync.exporter.wealthfolio.exporter.WealthfolioExporter",
        StubWealthfolioExporter,
    )
    monkeypatch.setattr(
        "finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter",
        StubActualBudgetExporter,
    )


# ── Canonical-datalake helpers ────────────────────────────────────────


async def _seed_canonical_accounts(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> list[str]:
    """Insert two canonical accounts (with transactions) so a destination
    has data to scope; returns their ids."""
    async with session_factory() as session:
        rows: list[Account] = []
        for idx, name in enumerate(["E2E Checking", "E2E Savings"]):
            acct = Account(
                provider_key="bunq",
                external_account_id=f"ext_{idx}",
                tenant_id=tenant_id,
                name=name,
                account_type="checking" if idx == 0 else "savings",
                currency_code="EUR",
                current_balance=Decimal("100.00"),
                is_active=True,
            )
            rows.append(acct)
            session.add(acct)
            # Flush so ``acct.id`` is materialised (Python-side ``uuid4``
            # default runs at flush time) before transactions reference it.
            await session.flush()
            for txn_idx in range(2):
                session.add(
                    Transaction(
                        tenant_id=tenant_id,
                        account_id=acct.id,
                        amount=Decimal("-10.00"),
                        currency_code="EUR",
                        occurred_at=datetime.now(UTC),
                        booked_at=datetime.now(UTC),
                        description=f"T{idx}{txn_idx}",
                        transaction_type="payment",
                        status="booked",
                        provider_key="bunq",
                        external_transaction_id=f"ext_t_{idx}_{txn_idx}",
                    )
                )
        await session.commit()
        return [str(a.id) for a in rows]


# ── Tests ─────────────────────────────────────────────────────────────


class TestEmptyInstallNoDestinationsRequired:
    """The personal datalake works fully without any destination."""

    async def test_list_types_and_no_destinations(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        types = await client.get(
            "/api/v1/destinations/types", headers=seeded_tenant["headers"]
        )
        assert types.status_code == 200
        keys = {t["key"] for t in types.json()}
        assert keys == {
            "wealthfolio",
            "actual-budget",
            "jupyter",
            "firefly",
            "ghostfolio",
            "investbrain",
        }

        empty = await client.get(
            "/api/v1/destinations", headers=seeded_tenant["headers"]
        )
        assert empty.status_code == 200
        assert empty.json() == []


class TestWizardLifecycle:
    """Create → test → discovery → preview → activate → run → pause → delete."""

    async def test_full_wizard_lifecycle(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_tenant: dict[str, str],
    ) -> None:
        tenant_id = seeded_tenant["tenant_id"]
        headers = seeded_tenant["headers"]
        acct_ids = await _seed_canonical_accounts(session_factory, tenant_id)

        # 1. Create a draft AB destination with an encrypted secret.
        draft = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="AB E2E",
            configuration={
                "server_url": AB_SERVER_URL,
                "default_off_budget": True,
            },
            secret={"password": "ab-secret", "encryption_password": "enc"},
            selected_account_ids=[acct_ids[0]],
            datasets=["accounts", "transactions"],
        )
        assert draft["status"] == TARGET_DRAFT
        assert draft["has_secret"] is True
        # The secret itself is never echoed by the API.
        assert "password" not in str(draft)

        target_id = draft["id"]

        # 2. Actual Budget budget discovery (read-only).
        discovery = await client.post(
            f"/api/v1/destinations/{target_id}/actual-budgets",
            headers=headers,
        )
        assert discovery.status_code == 200
        assert discovery.json()["budgets"] == StubActualBudgetClient.DISCOVERED

        # 3. Test connection (test-before-write contract).
        tested = await client.post(
            f"/api/v1/destinations/{target_id}/test", headers=headers
        )
        assert tested.status_code == 200
        assert tested.json()["status"] == "ready"

        # 4. Mapping/data preview (describes the existing AB account).
        preview = await client.post(
            f"/api/v1/destinations/{target_id}/preview", headers=headers
        )
        assert preview.status_code == 200
        pbody = preview.json()
        assert pbody["remote_accounts_read"] is True
        assert pbody["account_count"] == 1  # only the selected account
        mapped = {a["name"]: a["action"] for a in pbody["accounts"]}
        assert mapped["E2E Checking"] == "use_existing"

        # 5. Activate → creates an export schedule.
        activated = await client.post(
            f"/api/v1/destinations/{target_id}/activate", headers=headers
        )
        assert activated.status_code == 200
        assert activated.json()["target"]["status"] == TARGET_ACTIVE
        assert activated.json()["target"]["schedule_id"] is not None

        # 6. Run now (manual) → completes via the stubbed exporter.
        run = await client.post(
            f"/api/v1/destinations/{target_id}/run", headers=headers
        )
        assert run.status_code == 200
        assert run.json()["status"] == "completed"

        # 7. Pause → stops future scheduled runs.
        paused = await client.post(
            f"/api/v1/destinations/{target_id}/pause", headers=headers
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == TARGET_PAUSED

        # 8. Run now while paused → blocked with a clear error.
        blocked = await client.post(
            f"/api/v1/destinations/{target_id}/run", headers=headers
        )
        assert blocked.status_code == 409

        # 9. Delete → destination gone but canonical data intact.
        deleted = await client.delete(
            f"/api/v1/destinations/{target_id}", headers=headers
        )
        assert deleted.status_code == 204

        async with session_factory() as session:
            remaining = await session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.tenant_id == tenant_id)
            )
            assert remaining == 2  # canonical accounts never removed

    async def test_jupyter_least_privilege_round_trip(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        jupyter = await create_destination(
            client,
            headers,
            target_type="jupyter",
            display_name="Notebook",
        )
        target_id = jupyter["id"]

        activated = await client.post(
            f"/api/v1/destinations/{target_id}/activate", headers=headers
        )
        assert activated.status_code == 200
        body = activated.json()
        assert body["target"]["status"] == TARGET_ACTIVE
        bootstrap = body["jupyter_bootstrap"]
        assert bootstrap["api_key"]
        assert "token" in bootstrap["notebook"].lower()

        # Rotate: the old key is deactivated and a new plaintext is issued.
        rotated = await client.post(
            f"/api/v1/destinations/{target_id}/jupyter-key/rotate",
            headers=headers,
        )
        assert rotated.status_code == 200
        assert rotated.json()["api_key"] != bootstrap["api_key"]

        # Jupyter has no export run; running it is rejected.
        blocked = await client.post(
            f"/api/v1/destinations/{target_id}/run", headers=headers
        )
        assert blocked.status_code == 409

        # The notebook download is credential-free and versioned.
        notebook = await client.get(
            f"/api/v1/destinations/{target_id}/jupyter-notebook",
            headers=headers,
        )
        assert notebook.status_code == 200
        assert "consumer contract v1" in notebook.text
        assert "POST" not in notebook.text
        assert "Authorization" not in notebook.text

    async def test_wrong_type_discovery_and_url_validation(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        """Type guard, missing-secret guard and URL/TLS validation reject
        invalid destinations before any external write."""
        headers = seeded_tenant["headers"]

        # Non-Actual-Budget destination → discovery rejected with 409.
        wf = await create_destination(
            client,
            headers,
            target_type="wealthfolio",
            display_name="WF",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )
        rejected = await client.post(
            f"/api/v1/destinations/{wf['id']}/actual-budgets", headers=headers
        )
        assert rejected.status_code == 409

        # Actual-Budget draft without a secret → discovery rejected 422.
        ab = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="No Secret",
            configuration={"server_url": AB_SERVER_URL},
        )
        missing = await client.post(
            f"/api/v1/destinations/{ab['id']}/actual-budgets", headers=headers
        )
        assert missing.status_code == 422

        # Public HTTP URL is rejected.
        bad = await client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "target_type": "actual-budget",
                "display_name": "Public",
                "configuration": {"server_url": "http://budget.example.com"},
                "secret": {"password": "pw"},
            },
        )
        assert bad.status_code == 422


class TestMultiDestinationIsolation:
    """Multiple destinations of the same type run independently."""

    async def test_two_actual_budget_destinations_share_no_state(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        tenant_id = seeded_tenant["tenant_id"]
        await _seed_canonical_accounts(session_factory, tenant_id)

        ids: list[str] = []
        for name in ("AB One", "AB Two"):
            draft = await create_destination(
                client,
                headers,
                target_type="actual-budget",
                display_name=name,
                configuration={"server_url": AB_SERVER_URL},
                secret={"password": "pw"},
                datasets=["accounts", "transactions"],
            )
            await client.post(
                f"/api/v1/destinations/{draft['id']}/activate", headers=headers
            )
            ids.append(draft["id"])

        # Both destinations exist and are active simultaneously.
        listed = await client.get("/api/v1/destinations", headers=headers)
        assert listed.status_code == 200
        assert {d["id"] for d in listed.json()} == set(ids)
        assert all(d["status"] == TARGET_ACTIVE for d in listed.json())

        # Each runs independently.
        for target_id in ids:
            run = await client.post(
                f"/api/v1/destinations/{target_id}/run", headers=headers
            )
            assert run.status_code == 200

        # Delete one; the other stays active.
        gone = await client.delete(
            f"/api/v1/destinations/{ids[0]}", headers=headers
        )
        assert gone.status_code == 204
        listed_after = await client.get("/api/v1/destinations", headers=headers)
        assert {d["id"] for d in listed_after.json()} == {ids[1]}
        assert listed_after.json()[0]["status"] == TARGET_ACTIVE
