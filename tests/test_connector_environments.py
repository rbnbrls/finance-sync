"""Environment policy tests for connector configuration and staging data."""

# pyright: basic

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from finance_sync.api.v1.sync import _decrypt_config
from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.connectors.bunq import BunqConnector
from finance_sync.connectors.environment import staging_connector_config
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.trading212 import _auth_headers
from finance_sync.services.auth import encrypt_credential

_SECRET = SecretStr("test-secret-key-at-least-16-chars")
_MASTER_KEY = SecretStr("ab" * 32)


def _settings(environment: str) -> Settings:
    return Settings.model_validate(
        {
            "APP_ENVIRONMENT": environment,
            "database_url": None,
            "redis_url": None,
            "secret_key": _SECRET,
            "master_encryption_key": _MASTER_KEY,
            "DEBUG": False,
        }
    )


def test_staging_exposes_managed_bunq_and_trading212_test_data() -> None:
    with TestClient(create_app(_settings("staging"))) as client:
        response = client.get("/api/v1/connectors")
        assert response.status_code == 200
        connectors = {item["name"]: item for item in response.json()}

        for provider in ("bunq", "trading212"):
            assert (
                connectors[provider]["configuration_mode"] == "staging_choice"
            )
            source = connectors[provider]["option_fields"][0]
            assert source["key"] == "data_source"
            assert [choice["value"] for choice in source["choices"]] == [
                "static",
                "test_api",
            ]

        cash = client.get(
            "/api/v1/staging-providers/trading212/api/v0/equity/account/cash"
        )
        assert cash.status_code == 200
        assert cash.json()["currencyCode"] == "EUR"

        payments = client.get(
            "/api/v1/staging-providers/bunq/v1/monetary-account/9100001/payment"
        )
        assert payments.status_code == 200
        assert len(payments.json()["Response"]) == 31


def test_production_exposes_user_managed_api_fields_and_hides_fixtures() -> (
    None
):
    with TestClient(create_app(_settings("prod"))) as client:
        response = client.get("/api/v1/connectors")
        assert response.status_code == 200
        connectors = {item["name"]: item for item in response.json()}

        for provider in ("bunq", "trading212"):
            assert connectors[provider]["configuration_mode"] == "user"

        assert [
            field["key"] for field in connectors["bunq"]["credential_fields"]
        ] == ["api_key"]

        assert [
            field["key"]
            for field in connectors["trading212"]["credential_fields"]
        ] == ["api_key", "api_secret"]

        fixture = client.get(
            "/api/v1/staging-providers/trading212/api/v0/equity/account/cash"
        )
        assert fixture.status_code == 404


def test_sync_uses_frontend_saved_connector_options() -> None:
    settings = _settings("prod")
    encrypted, nonce = encrypt_credential(
        json.dumps({"api_key": "configured-in-frontend"}), settings
    )
    stored = SimpleNamespace(
        encrypted_payload=encrypted,
        nonce=nonce,
        description=json.dumps(
            {
                "base_url": "https://provider.example.test",
                "demo": True,
                "_label": "My connector",
            }
        ),
    )

    config = _decrypt_config(stored, "trading212", settings)  # type: ignore[arg-type]

    assert config.credentials == {"api_key": "configured-in-frontend"}
    assert config.options == {
        "base_url": "https://provider.example.test",
        "demo": True,
    }


def test_staging_test_api_endpoints_are_locked() -> None:
    settings = _settings("staging")
    bunq_credentials, bunq_options = staging_connector_config(
        "bunq",
        settings,
        data_source="test_api",
        credentials={"api_key": "sandbox-key"},
    )
    assert bunq_credentials == {"api_key": "sandbox-key"}
    assert bunq_options["base_url"] == (
        "https://public-api.sandbox.bunq.com/v1"
    )
    assert bunq_options["full_auth"] is True

    t212_credentials, t212_options = staging_connector_config(
        "trading212",
        settings,
        data_source="test_api",
        credentials={"api_key": "demo-key", "api_secret": "demo-secret"},
    )
    assert t212_credentials == {
        "api_key": "demo-key",
        "api_secret": "demo-secret",
    }
    assert t212_options["base_url"] == "https://demo.trading212.com"


def test_trading212_uses_basic_auth_for_current_key_pairs() -> None:
    assert _auth_headers("key", "secret") == {
        "Authorization": "Basic a2V5OnNlY3JldA=="
    }
    assert _auth_headers("legacy-key") == {"Authorization": "legacy-key"}


@pytest.mark.asyncio
async def test_bunq_sandbox_uses_full_signed_bootstrap() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(
                200,
                json={"Response": [{"Token": {"token": "install-token"}}]},
            )
        if request.url.path.endswith("/device-server"):
            return httpx.Response(200, json={"Response": [{"Id": {"id": 1}}]})
        if request.url.path.endswith("/session-server"):
            return httpx.Response(
                200,
                json={
                    "Response": [
                        {"Token": {"token": "session-token"}},
                        {"UserPerson": {"id": 42}},
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        base_url="https://public-api.sandbox.bunq.com/v1",
        transport=httpx.MockTransport(handler),
    )
    connector = BunqConnector(
        ConnectorConfig(
            provider_type="bunq",
            credentials={"api_key": "sandbox-key"},
            options={"full_auth": True},
        ),
        http_client=client,
    )

    await connector.authenticate()
    await client.aclose()

    assert [request.url.path for request in calls] == [
        "/v1/installation",
        "/v1/device-server",
        "/v1/session-server",
    ]
    for request in calls[1:]:
        assert request.headers["X-Bunq-Client-Authentication"] == (
            "install-token"
        )
        assert request.headers["X-Bunq-Client-Signature"]
