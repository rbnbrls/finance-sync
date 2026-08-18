"""Retirement contract for the global exporter HTTP API.

The old ``/api/v1/exporters/*`` surface is replaced by persisted
destinations (``/api/v1/destinations``).  Per the migration documented in
the destination-wizard backlog, the old endpoints keep a **clear migration
error** pointing at the destination API instead of silently failing.

These tests pin that contract: every retired endpoint returns ``410 Gone``
with a message that (a) names the destination API, (b) never echoes
credentials or secret payloads, and (c) does not run any exporter side
effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from finance_sync.app import create_app
from finance_sync.config.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

_TEST_SECRET: SecretStr = SecretStr("test-secret-key-at-least-16-chars")

#: Paths and methods of the retired global exporter surface.
_RETIRED_ROUTES = [
    ("/api/v1/exporters/types", "GET"),
    ("/api/v1/exporters/config", "GET"),
    ("/api/v1/exporters/export", "POST"),
    ("/api/v1/exporters/runs", "GET"),
    ("/api/v1/exporters/wealthfolio/runs/{id}/retry", "POST"),
    ("/api/v1/exporters/actual-budget/runs/{id}/retry", "POST"),
]


@pytest.fixture
def app() -> FastAPI:
    return create_app(
        settings=Settings(
            database_url=None, redis_url=None, secret_key=_TEST_SECRET
        )
    )


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path,method", _RETIRED_ROUTES)
def test_retired_exporter_route_returns_410_migration_error(
    client: TestClient,
    path: str,
    method: str,
) -> None:
    """Every retired exporter route 410s with a clear migration pointer."""
    response = client.request(
        method, path.format(id="00000000-0000-0000-0000-000000000000")
    )
    assert response.status_code == 410
    body = response.json()
    detail = str(body["detail"])
    # The migration error clearly names the persisted destination API.
    assert "/api/v1/destinations" in detail
    assert "retired" in detail.lower() or "destination" in detail.lower()


def test_retired_route_never_echoes_credentials(client: TestClient) -> None:
    """The migration error is static text; it must not reflect inputs or
    carry secret-like content."""
    response = client.post(
        "/api/v1/exporters/export",
        json={"password": "hunter2", "api_key": "not-relevant"},
        headers={"Authorization": "Bearer fake.token.value"},
    )
    assert response.status_code == 410
    detail = str(response.json()["detail"])
    assert "hunter2" not in detail
    assert "not-relevant" not in detail
    assert "fake.token.value" not in detail


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_retired_route_rejects_all_methods(
    client: TestClient, method: str
) -> None:
    """All HTTP verbs on the retired surface resolve to the same 410."""
    response = client.request(method, "/api/v1/exporters/something")
    assert response.status_code == 410
