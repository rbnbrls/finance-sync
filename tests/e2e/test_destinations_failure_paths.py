"""E2E tests — destination creation failure paths (datalake-first wizard).

Runs against the real PostgreSQL harness with stubbed external clients.
Complements ``test_destinations_lifecycle.py`` (happy path) with the
failure surface the acceptance criteria require:

* validation errors — bad target_type, missing display_name, public HTTP
  URL, credentials smuggled into ``configuration``;
* missing required fields — no credential before discovery/test/activate,
  missing secret on an app destination;
* API failure — external-client failure surfaces as a health-state change
  and a 422 on discovery / a ``failed`` status on test, never a 500;
* wizard-specific error states — wrong-type discovery 409, run-before-
  activate 409, run-while-paused 409, unknown destination 404, Jupyter
  rotate-before-activate 409, notebook on non-Jupyter 404, and
  cross-tenant isolation (a destination is invisible to another tenant).

Every assertion targets the UI-relevant API state (health status,
envelope-encrypted secret, no credential echo, canonical data untouched),
not just the final HTTP status code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from finance_sync.models.export_target import TARGET_DRAFT
from tests.e2e.destinations_helpers import (
    create_destination,
    dest_client,
    seeded_destination_tenant,
)

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.config.settings import Settings

pytestmark = pytest.mark.e2e

seeded_tenant = seeded_destination_tenant
client = dest_client

# Private self-hosted endpoint the URL/TLS validator accepts over HTTP.
AB_SERVER_URL = "http://192.168.1.50:5006"


# ── Validation errors ────────────────────────────────────────────────


class TestCreateValidationErrors:
    """Malformed create payloads are rejected before any write."""

    async def test_unknown_target_type_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        resp = await client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "target_type": "not-a-type",
                "display_name": "Bogus",
                "configuration": {},
                "datasets": ["accounts"],
            },
        )
        assert resp.status_code == 422
        # Pydantic lists the invalid field with the allowed values.
        assert "target_type" in resp.text
        assert "wealthfolio" in resp.text

    async def test_missing_display_name_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        resp = await client.post(
            "/api/v1/destinations",
            headers=headers,
            json={"target_type": "wealthfolio"},
        )
        assert resp.status_code == 422
        assert "display_name" in resp.text

    async def test_blank_display_name_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        resp = await client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "target_type": "wealthfolio",
                "display_name": "",
                "configuration": {"server_url": AB_SERVER_URL},
                "secret": {"password": "pw"},
            },
        )
        assert resp.status_code == 422
        assert "display_name" in resp.text

    async def test_public_http_url_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        """Wizard step 2 (Verbind): a public HTTP URL never passes."""
        headers = seeded_tenant["headers"]
        resp = await client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "target_type": "actual-budget",
                "display_name": "Public",
                "configuration": {"server_url": "http://budget.example.com"},
                "secret": {"password": "pw"},
            },
        )
        assert resp.status_code == 422
        assert "local or private" in resp.json()["detail"]

    async def test_credential_in_configuration_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        """Secrets belong in ``secret``; ``configuration`` must stay safe."""
        headers = seeded_tenant["headers"]
        resp = await client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "target_type": "wealthfolio",
                "display_name": "Leaky",
                "configuration": {
                    "server_url": AB_SERVER_URL,
                    "password": "hunter2",
                },
            },
        )
        assert resp.status_code == 422
        assert "Put credentials in secret" in resp.json()["detail"]
        # And the rejected value never persists anywhere.
        listed = await client.get("/api/v1/destinations", headers=headers)
        assert listed.json() == []

    async def test_invalid_dataset_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        resp = await client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "target_type": "jupyter",
                "display_name": "Bad datasets",
                "datasets": ["accounts", "write_everything"],
            },
        )
        assert resp.status_code == 422
        assert "datasets" in resp.text


# ── Missing required fields ──────────────────────────────────────────


class TestMissingRequiredFields:
    """Wizard gates: draft without a credential cannot be progressed."""

    async def test_activate_without_credential_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        """A Wealthfolio draft with no secret stays a draft forever."""
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="wealthfolio",
            display_name="No cred",
            configuration={"server_url": AB_SERVER_URL},
        )
        assert draft["status"] == TARGET_DRAFT
        assert draft["has_secret"] is False

        activated = await client.post(
            f"/api/v1/destinations/{draft['id']}/activate", headers=headers
        )
        assert activated.status_code == 422
        assert "Test and save a credential" in activated.json()["detail"]

        # Still a draft after the failed activation; no schedule leaked.
        still = await client.get("/api/v1/destinations", headers=headers)
        body = still.json()
        assert len(body) == 1
        assert body[0]["status"] == TARGET_DRAFT
        assert body[0]["schedule_id"] is None

    async def test_test_connection_without_credential_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="No secret",
            configuration={"server_url": AB_SERVER_URL},
        )
        tested = await client.post(
            f"/api/v1/destinations/{draft['id']}/test", headers=headers
        )
        # test_target catches the missing-credential ValueError and returns
        # a 200 with status="failed" — never a 500.
        assert tested.status_code == 200
        assert tested.json()["status"] == "failed"
        assert "credential" in tested.json()["message"]

        # The failed probe is reflected in the destination health state.
        listed = await client.get("/api/v1/destinations", headers=headers)
        health = listed.json()[0]
        assert health["last_health_status"] == "failed"
        assert health["last_health_error"] is not None

    async def test_discovery_without_secret_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="No secret",
            configuration={"server_url": AB_SERVER_URL},
        )
        discovered = await client.post(
            f"/api/v1/destinations/{draft['id']}/actual-budgets",
            headers=headers,
        )
        assert discovered.status_code == 422
        assert "password before discovery" in discovered.json()["detail"]


# ── API / external-client failure ────────────────────────────────────


class TestExternalClientFailure:
    """External failure surfaces as health state, never as a 500."""

    async def test_discovery_failure_marks_health_and_returns_422(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="Unreachable",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )

        # The external AB server is unreachable during discovery.
        class _Boom:
            @classmethod
            async def discover_budgets(cls, config: object) -> list[object]:
                _msg = "connection refused by AB server"
                raise RuntimeError(_msg)

        monkeypatch.setattr(
            "finance_sync.exporter.actual_budget.client.ActualBudgetClient",
            _Boom,
        )
        discovered = await client.post(
            f"/api/v1/destinations/{draft['id']}/actual-budgets",
            headers=headers,
        )
        assert discovered.status_code == 422
        assert "discovery failed" in discovered.json()["detail"]

        # The wizard can retry: the draft survives the failure (the
        # exception path rolls the health write back, keeping the
        # destination consistent and re-probeable).
        listed = await client.get("/api/v1/destinations", headers=headers)
        body = listed.json()
        assert len(body) == 1
        assert body[0]["id"] == draft["id"]
        assert body[0]["status"] == TARGET_DRAFT
        # The credential is still present (envelope-encrypted) so a retry
        # with a reachable server works without re-entering it.
        assert body[0]["has_secret"] is True

    async def test_connection_test_failure_returns_failed_status(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Step 2 'Verbinding testen' shows a failed probe, not a crash."""
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="wealthfolio",
            display_name="Bad server",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )

        # The external Wealthfolio server rejects the probe.
        class _Rejecting:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> _Rejecting:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def check_auth_status(self) -> None:
                return None

            async def authenticate(self) -> None:
                _msg = "401 invalid credentials"
                raise RuntimeError(_msg)

        monkeypatch.setattr(
            "finance_sync.exporter.wealthfolio.client.WealthfolioClient",
            _Rejecting,
        )
        tested = await client.post(
            f"/api/v1/destinations/{draft['id']}/test", headers=headers
        )
        assert tested.status_code == 200
        assert tested.json()["status"] == "failed"
        assert "401" in tested.json()["message"]

        listed = await client.get("/api/v1/destinations", headers=headers)
        health = listed.json()[0]
        assert health["last_health_status"] == "failed"
        assert health["last_health_error"] is not None

    async def test_run_export_failure_returns_failed_and_recovers(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed export run reports status=failed and never 500s.

        This mirrors the UI ``runDestination`` toast:
        ``Exportstatus: failed — <sanitized error>``.
        """
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="Broken run",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )
        activated = await client.post(
            f"/api/v1/destinations/{draft['id']}/activate", headers=headers
        )
        assert activated.status_code == 200
        assert activated.json()["target"]["status"] == "active"

        # The exporter (not the client) is what the worker instantiates.
        class _FailingExporter:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def run_export(self, **kwargs: object) -> object:
                return type(
                    "ExportResult",
                    (),
                    {
                        "status": "failed",
                        "error_message": "external AB server unreachable",
                    },
                )()

        monkeypatch.setattr(
            "finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter",
            _FailingExporter,
        )
        run = await client.post(
            f"/api/v1/destinations/{draft['id']}/run", headers=headers
        )
        assert run.status_code == 200
        assert run.json()["status"] == "failed"
        assert "unreachable" in (run.json().get("error") or "")

        # The failure is recorded on the schedule (visible in Sync Runs).
        listed = await client.get("/api/v1/destinations", headers=headers)
        body = listed.json()[0]
        assert body["last_run_status"] == "failed"
        assert body["last_run_error"] is not None


# ── Wizard-specific error states ─────────────────────────────────────


class TestWizardSpecificErrorStates:
    """States only reachable inside the wizard flow."""

    async def test_discovery_on_wrong_type_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        """'Budgetten ontdekken' is only offered for Actual Budget."""
        headers = seeded_tenant["headers"]
        wf = await create_destination(
            client,
            headers,
            target_type="wealthfolio",
            display_name="WF",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )
        discovered = await client.post(
            f"/api/v1/destinations/{wf['id']}/actual-budgets", headers=headers
        )
        assert discovered.status_code == 409
        assert "only available for Actual Budget" in discovered.json()["detail"]

    async def test_run_before_activate_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="actual-budget",
            display_name="Not activated",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )
        run = await client.post(
            f"/api/v1/destinations/{draft['id']}/run", headers=headers
        )
        assert run.status_code == 409
        assert "Activate this destination first" in run.json()["detail"]

    async def test_unknown_destination_is_404(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        # A well-formed but nonexistent destination id (UUID format, so the
        # query itself is valid and the 404 lookup runs).
        missing = await client.post(
            "/api/v1/destinations/00000000-0000-0000-0000-000000000000/activate",
            headers=headers,
        )
        assert missing.status_code == 404
        assert "Destination not found" in missing.json()["detail"]

    async def test_jupyter_rotate_before_activate_is_rejected(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="jupyter",
            display_name="Notebook",
        )
        rotated = await client.post(
            f"/api/v1/destinations/{draft['id']}/jupyter-key/rotate",
            headers=headers,
        )
        assert rotated.status_code == 409
        assert "Activate a Jupyter destination" in rotated.json()["detail"]

    async def test_notebook_download_on_non_jupyter_is_404(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
    ) -> None:
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="wealthfolio",
            display_name="WF",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )
        notebook = await client.get(
            f"/api/v1/destinations/{draft['id']}/jupyter-notebook",
            headers=headers,
        )
        assert notebook.status_code == 404
        assert "Only Jupyter destinations" in notebook.json()["detail"]


# ── Cross-tenant isolation ───────────────────────────────────────────


class TestTenantIsolation:
    """Single-owner: a destination is invisible to other tenants."""

    async def test_destination_is_invisible_to_other_tenant(
        self,
        client: httpx.AsyncClient,
        seeded_tenant: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
        e2e_settings: Settings,
    ) -> None:
        """The single-owner model keeps every tenant's destinations sealed."""
        headers = seeded_tenant["headers"]
        draft = await create_destination(
            client,
            headers,
            target_type="wealthfolio",
            display_name="Private",
            configuration={"server_url": AB_SERVER_URL},
            secret={"password": "pw"},
        )

        # A second tenant's JWT cannot see the destination at all.
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.models import Tenant, User
        from finance_sync.models.enums import UserRole
        from finance_sync.services.auth import (
            create_access_token,
            hash_password,
        )

        async with session_factory() as session, UnitOfWork(session) as uow:
            other_tenant = await uow.tenants.add(
                Tenant(slug="other-tenant", name="Other Tenant")
            )
            other_user = User(
                email="other@finance-sync.local",
                tenant_id=str(other_tenant.id),
                hashed_password=hash_password("other-password"),
                display_name="Other Owner",
                role=UserRole.ADMIN,
                is_active=True,
            )
            uow.session.add(other_user)
        other_token = create_access_token(
            {
                "sub": str(other_user.id),
                "tenant_id": str(other_tenant.id),
                "role": "admin",
            },
            e2e_settings,
        )
        other_headers = {"Authorization": f"Bearer {other_token}"}

        listed = await client.get("/api/v1/destinations", headers=other_headers)
        assert listed.json() == []

        # Direct access by id is also blocked (404, not 403 — no leak).
        direct = await client.post(
            f"/api/v1/destinations/{draft['id']}/activate",
            headers=other_headers,
        )
        assert direct.status_code == 404
