"""OpenAPI document covers the multi-connection acceptance criteria.

The multi-connection story (t_7d8bc1f2) requires the **OpenAPI
specification** to document the new connection model, account selection,
pause/resume, manual sync, scheduler behaviour and error recovery.  The
spec is generated from the FastAPI route definitions
(``app.openapi()`` — served live at ``/openapi.json``), so this module
pins that coverage in CI: every multi-connection path must exist with
stable operation ids, and the route docstrings must describe the
acceptance-criteria concepts.

A documentation-only regression here fails fast without needing a
database: generation imports the app without starting the lifespan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

from finance_sync.app import create_app
from finance_sync.config.settings import Settings

_TEST_SECRET = "test-secret-key-at-least-16-chars"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        access_token_expire_minutes=15,
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings=settings)


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def _paths(client: TestClient) -> dict[str, Any]:
    return client.get("/openapi.json").json()["paths"]


def _op(client: TestClient, path: str, method: str) -> dict[str, Any]:
    return _paths(client)[path][method]


def _doc(op: dict[str, Any]) -> str:
    """Normalised (whitespace-collapsed, lowercased) summary+description."""
    raw = f"{op.get('summary', '')} {op.get('description', '')}"
    return " ".join(raw.split()).lower()


# ── Endpoint surface ──────────────────────────────────────────────────


class TestMultiConnectionOpenApiPaths:
    """Every multi-connection operation is present with a stable id."""

    CONNECTOR_PATHS = {
        ("get", "/api/v1/connectors/configs", "list_connector_configs"),
        (
            "get",
            "/api/v1/connectors/configs/{config_id}/deletion-preview",
            "preview_connector_deletion",
        ),
        ("post", "/api/v1/connectors/configs", "create_connector_config"),
        (
            "get",
            "/api/v1/connectors/configs/{config_id}",
            "get_connector_config",
        ),
        (
            "put",
            "/api/v1/connectors/configs/{config_id}",
            "update_connector_config",
        ),
        (
            "delete",
            "/api/v1/connectors/configs/{config_id}",
            "delete_connector_config",
        ),
        (
            "post",
            "/api/v1/connectors/configs/{config_id}/test",
            "test_connector_connection",
        ),
        (
            "post",
            "/api/v1/connectors/{connection_id}/reauthenticate",
            "reauthenticate_connector",
        ),
        (
            "get",
            "/api/v1/connectors/{connection_id}/health",
            "get_connection_health",
        ),
        (
            "post",
            "/api/v1/connectors/configs/{config_id}/pause",
            "pause_connector_connection",
        ),
        (
            "post",
            "/api/v1/connectors/configs/{config_id}/resume",
            "resume_connector_connection",
        ),
        (
            "post",
            "/api/v1/connectors/configs/{config_id}/accounts",
            "set_connection_accounts",
        ),
        ("get", "/api/v1/connectors/audit-log", "list_connection_audit"),
        (
            "post",
            "/api/v1/sync/connections/{connection_id}",
            "trigger_sync_connection",
        ),
    }

    def test_multi_connection_operations_registered(
        self, client: TestClient
    ) -> None:
        paths = _paths(client)
        for method, path, operation_id in self.CONNECTOR_PATHS:
            assert path in paths, f"{method.upper()} {path} missing"
            assert method in paths[path], f"{method.upper()} {path} missing"
            actual = paths[path][method]["operationId"]
            assert actual.startswith(operation_id), (
                f"{method.upper()} {path} operationId drift: {actual}"
            )

    def test_connector_catalog_endpoint_is_documented(
        self, client: TestClient
    ) -> None:
        paths = _paths(client)
        assert "/api/v1/connectors/catalog" in paths
        operation = paths["/api/v1/connectors/catalog"]["get"]
        assert operation["tags"] == ["connectors"]
        assert "secret" in operation["description"].lower()

    def test_unified_file_dispatch_endpoints_are_documented(
        self, client: TestClient
    ) -> None:
        paths = _paths(client)
        dispatch = paths["/api/v1/connectors/file-uploads/dispatch"]
        confirm = paths[
            "/api/v1/connectors/file-uploads/dispatch/{run_id}/confirm"
        ]
        assert "post" in dispatch
        assert "post" in confirm
        assert "stable contract" in dispatch["post"]["description"]

    def test_upload_history_schema_is_provider_neutral(
        self, client: TestClient
    ) -> None:
        response = _op(client, "/api/v1/connectors/file-uploads/runs", "get")
        schema_ref = response["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["items"]["$ref"]
        assert schema_ref.endswith("/FileUploadRunResponse")
        properties = client.get("/openapi.json").json()["components"][
            "schemas"
        ]["FileUploadRunResponse"]["properties"]
        for field in (
            "provider_type",
            "profile_name",
            "period_start",
            "warnings",
            "retryable",
        ):
            assert field in properties

    def test_connectors_config_listing_describes_connections(
        self, client: TestClient
    ) -> None:
        doc = _doc(_op(client, "/api/v1/connectors/configs", "get"))
        assert "connection" in doc
        assert "multiple connections per provider" in doc

    def test_manual_sync_endpoint_documented(self, client: TestClient) -> None:
        doc = _doc(
            _op(client, "/api/v1/sync/connections/{connection_id}", "post")
        )
        assert "connection_id" in doc
        assert "single connection" in doc

    def test_connector_paths_tagged_connectors(
        self, client: TestClient
    ) -> None:
        paths = _paths(client)
        for path in (
            "/api/v1/connectors/configs",
            "/api/v1/connectors/configs/{config_id}",
            "/api/v1/connectors/configs/{config_id}/pause",
            "/api/v1/connectors/audit-log",
        ):
            for method in ("get", "post", "put", "delete"):
                if method in paths[path]:
                    assert paths[path][method]["tags"] == ["connectors"], (
                        f"{method.upper()} {path} tags drift"
                    )


# ── Acceptance-criteria documentation coverage ────────────────────────


class TestMultiConnectionOpenApiDocumentation:
    """Route docstrings must cover the story's acceptance criteria."""

    def test_connection_model_documented(self, client: TestClient) -> None:
        """Stable connection id + per-connection status/label/selection."""
        doc = _doc(_op(client, "/api/v1/connectors/configs/{config_id}", "get"))
        assert "connection_id" in doc
        assert "connection" in doc

        # The response schema documents the connection attributes.
        paths = _paths(client)
        resp_schema = paths["/api/v1/connectors/configs/{config_id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        ref = resp_schema.get("$ref", "")
        assert "ConnectorConfigResponse" in ref
        schema = client.get("/openapi.json").json()["components"]["schemas"][
            "ConnectorConfigResponse"
        ]
        props = schema.get("properties", {})
        for field in (
            "connection_id",
            "description",
            "status",
            "selected_accounts",
            "last_attempt_at",
            "last_success_at",
            "last_error",
        ):
            assert field in props, f"ConnectorConfigResponse.{field} missing"
        assert "credentials" not in props, (
            "the response schema must never expose credentials"
        )

    def test_account_selection_documented(self, client: TestClient) -> None:
        """Only selected accounts sync; history survives selection changes."""
        doc = _doc(
            _op(
                client,
                "/api/v1/connectors/configs/{config_id}/accounts",
                "post",
            )
        )
        assert "select" in doc
        assert "selected accounts" in doc
        assert "history" in doc
        assert "purge" in doc

    def test_pause_resume_documented(self, client: TestClient) -> None:
        """Pausing stops automatic syncs; existing data is kept."""
        pause_doc = _doc(
            _op(
                client,
                "/api/v1/connectors/configs/{config_id}/pause",
                "post",
            )
        )
        assert "pause" in pause_doc
        assert "scheduler" in pause_doc
        assert "skip" in pause_doc

        resume_doc = _doc(
            _op(
                client,
                "/api/v1/connectors/configs/{config_id}/resume",
                "post",
            )
        )
        assert "resume" in resume_doc
        assert "paused" in resume_doc

    def test_scheduler_behaviour_documented(self, client: TestClient) -> None:
        """All active connections run independently; failures isolate."""
        doc = _doc(_op(client, "/api/v1/sync", "post"))
        assert "independently" in doc
        assert "failing connection never blocks" in doc
        assert "paused connections are skipped" in doc

    def test_error_recovery_documented(self, client: TestClient) -> None:
        """Sanitised last_error + redacted credentials on failure."""
        doc = _doc(
            _op(
                client,
                "/api/v1/connectors/configs/{config_id}/test",
                "post",
            )
        )
        assert "last_error" in doc
        assert "sanitised" in doc
        assert "audit" in doc

        list_doc = _doc(_op(client, "/api/v1/connectors/configs", "get"))
        assert "credentials are never included" in list_doc

    def test_audit_log_documented(self, client: TestClient) -> None:
        """Admin-only, tenant-scoped audit trail without secrets."""
        doc = _doc(_op(client, "/api/v1/connectors/audit-log", "get"))
        assert "admin" in doc
        assert "audit" in doc
        assert "never contain" in doc

    def test_security_schemes_required(self, client: TestClient) -> None:
        """Connector + sync endpoints require bearer auth."""
        for path, method in (
            ("/api/v1/connectors/configs", "get"),
            ("/api/v1/connectors/configs", "post"),
            ("/api/v1/connectors/audit-log", "get"),
            ("/api/v1/sync/connections/{connection_id}", "post"),
        ):
            security = _op(client, path, method).get("security")
            assert security, f"{method.upper()} {path} has no security scheme"
            schemes = {next(iter(req.keys())) for req in security}
            assert "HTTPBearer" in schemes, (
                f"{method.upper()} {path} security schemes: {schemes}"
            )
