"""Multi-connection management tests for the connectors config API.

Covers the "meerdere instellingsverbindingen en accountselectie" story's
API surface: multiple connections per provider, tenant-scoped CRUD by
``connection_id`` (list / get / create / rename / update / delete),
pause / resume, account selection, per-connection status fields,
sanitised errors — and, critically, that credentials never leak into any
response body.

Uses the mock-session harness style of ``test_connectors_auth`` plus a
stateful in-memory fake session so create/update/delete flows behave like
a real database (ids are generated on add, rows are queryable afterwards,
tenant filtering mirrors the WHERE clauses the API always applies).
"""

# pyright: basic

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import BinaryExpression

from finance_sync.api.deps.auth import AuthContext, get_auth_context
from finance_sync.api.v1 import connectors_config as cc
from finance_sync.app import create_app
from finance_sync.config.environments import Environment
from finance_sync.config.settings import Settings
from finance_sync.connectors.models import ConnectorHealth, RawAccount
from finance_sync.dependencies import get_db
from finance_sync.models.credential import Credential
from finance_sync.services.auth import encrypt_credential

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

_TENANT = "tenant-1"
_OTHER_TENANT = "tenant-2"
_CONFIGS_URL = "/api/v1/connectors/configs"
#: 64 hex chars = 32 bytes → valid AES-256 master key for the test harness.
_MASTER_KEY = "a1b2c3d4" * 8


# ── Harness ────────────────────────────────────────────────────────────


def _settings() -> Settings:
    return Settings(
        environment=Environment.PRODUCTION,
        database_url=None,
        redis_url="redis://localhost:6379/0",
        secret_key="test-production-secret-key-1234",
        cors_origins=["https://example.test"],
        master_encryption_key=SecretStr(_MASTER_KEY),
    )


def _user_ctx(role: str = "admin") -> AuthContext:
    """JWT-style principal carrying a role and tenant."""
    user = MagicMock()
    user.role = role
    user.tenant_id = _TENANT
    user.id = "user-1"
    return AuthContext(user=user)


def _make_cred(
    provider: str,
    *,
    connection_id: str,
    label: str | None = None,
    status: str = "active",
    selected: list[str] | None = None,
    last_error: str | None = None,
    tenant: str = _TENANT,
) -> Credential:
    """A Credential row with the attributes the API serialises."""
    cred = Credential(
        tenant_id=tenant,
        provider_key=provider,
        encrypted_payload=b"\x00\x01",
        nonce=b"\x00" * 12,
        description=(f'{{"_label": "{label}"}}' if label else "{}"),
        status=status,
        selected_accounts=selected,
        last_error=last_error,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    cred.id = connection_id
    return cred


def _encrypted_cred(
    provider: str,
    *,
    connection_id: str,
    credentials: dict[str, str],
    label: str | None = None,
    tenant: str = _TENANT,
) -> Credential:
    """A Credential row whose payload is real AES-256-GCM ciphertext."""
    plaintext = json.dumps(credentials, separators=(",", ":"))
    ciphertext, nonce = encrypt_credential(plaintext, _settings())
    cred = _make_cred(
        provider,
        connection_id=connection_id,
        label=label,
        tenant=tenant,
    )
    cred.encrypted_payload = ciphertext
    cred.nonce = nonce
    return cred


class _FakeSession:
    """In-memory stand-in for ``AsyncSession`` used by the API tests.

    Rows are pre-filtered by tenant (mirroring the ``tenant_id`` WHERE
    clauses the API always applies) and by credential ``id`` when a
    statement filters on it, so tenant scoping and by-id lookups behave
    like a real database.  ``add`` generates a UUID id exactly like the
    ORM's ``pk_uuid`` default, and ``delete`` removes the row so a later
    lookup 404s.
    """

    def __init__(
        self, rows: list[Credential], tenant: str | None = None
    ) -> None:
        self._tenant = tenant
        self._rows = [
            r for r in rows if tenant is None or r.tenant_id == tenant
        ]
        self._added: list[Any] = []

    # ── helpers ──────────────────────────────────────────────────────

    def _all_rows(self) -> list[Credential]:
        return self._rows + [
            obj for obj in self._added if isinstance(obj, Credential)
        ]

    @staticmethod
    def _credential_id_filters(stmt: Any) -> set[str]:
        """Extract ``Credential.id == <value>`` constraints from a stmt."""
        ids: set[str] = set()
        where = getattr(stmt, "whereclause", None)
        if where is None:
            return ids
        for node in visitors.iterate(where):
            if not isinstance(node, BinaryExpression):
                continue
            col = node.left
            if (
                getattr(col, "key", None) == "id"
                and getattr(getattr(col, "table", None), "name", None)
                == "credentials"
            ):
                value = getattr(node.right, "value", None)
                if value is not None:
                    ids.add(str(value))
        return ids

    @staticmethod
    def _targets_credentials(stmt: Any) -> bool:
        """True when the statement reads the ``credentials`` table."""
        where = getattr(stmt, "whereclause", None)
        if where is not None:
            for node in visitors.iterate(where):
                col = getattr(node, "left", None)
                if (
                    getattr(getattr(col, "table", None), "name", None)
                    == "credentials"
                ):
                    return True
        # Fall back to the select entities (e.g. a bare
        # ``select(Credential)`` without a where clause).
        for ent in getattr(stmt, "_raw_columns", []) or []:
            tbl = getattr(
                getattr(ent, "entity", None) or ent, "__table__", None
            )
            if getattr(tbl, "name", None) == "credentials":
                return True
        return False

    # ── async session surface ────────────────────────────────────────

    async def execute(self, stmt: Any) -> Any:
        rows = self._all_rows()
        # Only credentials live in this fake; a query targeting another
        # table (e.g. SyncSchedule) yields no rows — the schedule
        # lifecycle is covered by the real-PG integration tests.
        if not self._targets_credentials(stmt):
            rows = []
        else:
            wanted = self._credential_id_filters(stmt)
            if wanted:
                rows = [r for r in rows if r.id in wanted]
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(rows)
        result.scalar_one_or_none.return_value = rows[0] if rows else None
        return result

    async def scalars(self, stmt: Any) -> Any:
        result = MagicMock()
        result.all.return_value = []
        return result

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = f"conn-{uuid4().hex[:10]}"
        self._added.append(obj)

    async def delete(self, obj: Any) -> None:
        if obj in self._rows:
            self._rows.remove(obj)
        if obj in self._added:
            self._added.remove(obj)

    async def flush(self) -> None:
        pass


class _FakeConnector:
    """Returns per-credential accounts so two bunq connections differ."""

    def __init__(self, config: Any) -> None:
        self._config = config

    async def health(self) -> ConnectorHealth:
        return ConnectorHealth(healthy=True, message="ok", provider_type="bunq")

    async def fetch_accounts(self) -> list[RawAccount]:
        key = str(self._config.credentials.get("api_key", ""))
        if key == "secret-bunq-a":
            return [
                RawAccount(
                    external_account_id="acc-a1",
                    name="Bunq A Checking",
                    account_type="checking",
                    currency_code="EUR",
                    current_balance=Decimal(100),
                    available_balance=Decimal(100),
                    iso_currency_code="EUR",
                    provider_metadata={"iban": "NL01BUNQ0123456789"},
                ),
                RawAccount(
                    external_account_id="acc-a2",
                    name="Bunq A Savings",
                    account_type="savings",
                    currency_code="EUR",
                    current_balance=Decimal(200),
                    available_balance=Decimal(200),
                    iso_currency_code="EUR",
                    provider_metadata={"iban": "NL01BUNQ9876543210"},
                ),
            ]
        return [
            RawAccount(
                external_account_id="acc-b1",
                name="Bunq B Joint",
                account_type="checking",
                currency_code="EUR",
                current_balance=Decimal(300),
                available_balance=Decimal(300),
                iso_currency_code="EUR",
                provider_metadata={"iban": "NL01BUNQ5555555555"},
            ),
        ]


def _fake_registry() -> Any:
    """Registry stand-in whose connector branches on the stored api_key."""
    registry = MagicMock()
    registry.__contains__.return_value = True
    registry.get_connector.side_effect = lambda config: _FakeConnector(config)
    return registry


def _make_app(
    session: _FakeSession,
    auth_ctx: AuthContext,
    settings: Settings | None = None,
) -> FastAPI:
    app = create_app(settings or _settings())
    app.dependency_overrides[get_auth_context] = lambda ctx=auth_ctx: ctx
    app.dependency_overrides[get_db] = lambda: session
    return app


@contextmanager
def _client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ── Listing multiple connections ───────────────────────────────────────


def test_list_returns_multiple_connections_per_provider() -> None:
    """Two bunq connections + one trading212 connection are all listed."""
    session = _FakeSession(
        [
            _make_cred(
                "bunq", connection_id="conn-bunq-1", label="Persoonlijk"
            ),
            _make_cred("bunq", connection_id="conn-bunq-2", label="Zakelijk"),
            _make_cred("trading212", connection_id="conn-t212-1"),
        ],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.get(_CONFIGS_URL)
        assert resp.status_code == 200
        payload = resp.json()
        assert len(payload) == 3
        bunq = [c for c in payload if c["provider_type"] == "bunq"]
        assert len(bunq) == 2
        assert {c["id"] for c in bunq} == {"conn-bunq-1", "conn-bunq-2"}
        # Credentials never leak into responses.
        assert all(
            "credentials" not in c and "encrypted_payload" not in c
            for c in payload
        )
        # connection_id is exposed for scoping syncs.
        assert payload[0]["connection_id"] == payload[0]["id"]


def test_list_returns_only_tenant_connections() -> None:
    """Another tenant's connections are invisible (tenant-scoped query)."""
    session = _FakeSession(
        [
            _make_cred("bunq", connection_id="conn-mine"),
            _make_cred(
                "bunq",
                connection_id="conn-other",
                tenant=_OTHER_TENANT,
            ),
        ],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        payload = client.get(_CONFIGS_URL).json()
        assert [c["id"] for c in payload] == ["conn-mine"]


def test_list_serialises_connection_status_fields() -> None:
    """Per-connection status, selection and last-error are serialised."""
    session = _FakeSession(
        [
            _make_cred(
                "bunq",
                connection_id="conn-1",
                status="paused",
                selected=["acc_1", "acc_2"],
                last_error="Connection refused",
            )
        ],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        conn = client.get(_CONFIGS_URL).json()[0]
        assert conn["status"] == "paused"
        assert conn["selected_accounts"] == ["acc_1", "acc_2"]
        assert conn["last_error"] == "Connection refused"


# ── Get by connection_id ───────────────────────────────────────────────


def test_get_connection_by_id() -> None:
    session = _FakeSession(
        [_make_cred("bunq", connection_id="conn-1", label="Mijn bunq")],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.get(f"{_CONFIGS_URL}/conn-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "conn-1"
        assert body["connection_id"] == "conn-1"
        assert body["provider_type"] == "bunq"
        assert body["description"] == "Mijn bunq"
        assert "encrypted_payload" not in resp.text


def test_get_unknown_connection_404s() -> None:
    session = _FakeSession([], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        assert client.get(f"{_CONFIGS_URL}/nope").status_code == 404


def test_get_other_tenant_connection_404s() -> None:
    """Another tenant's connection_id must not resolve for tenant-1."""
    session = _FakeSession(
        [_make_cred("bunq", connection_id="conn-other", tenant=_OTHER_TENANT)],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.get(f"{_CONFIGS_URL}/conn-other")
        assert resp.status_code == 404


# ── Creating multiple connections per provider ─────────────────────────


def test_create_second_connection_for_same_provider_is_allowed() -> None:
    """POST /connectors/configs no longer 409s on a duplicate provider."""
    session = _FakeSession([], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            _CONFIGS_URL,
            json={
                "provider_type": "bunq",
                "credentials": {"api_key": "key-b"},
                "description": "Tweede bunq",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["provider_type"] == "bunq"
        assert body["status"] == "active"
        # The raw key must never appear in the response.
        assert "key-b" not in resp.text


def test_create_never_returns_credentials() -> None:
    """Created connections mask credentials and ciphertext in the body."""
    session = _FakeSession([], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            _CONFIGS_URL,
            json={
                "provider_type": "trading212",
                "credentials": {
                    "api_key": "super-secret-key-123",
                    "api_secret": "super-secret-secret-456",
                },
                "description": "T212",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["is_configured"] is True
        assert "super-secret-key-123" not in resp.text
        assert "super-secret-secret-456" not in resp.text
        assert "encrypted_payload" not in resp.text
        assert "nonce" not in resp.text


def test_create_unknown_provider_400s() -> None:
    session = _FakeSession([], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            _CONFIGS_URL,
            json={"provider_type": "not-a-provider", "credentials": {}},
        )
        assert resp.status_code == 400


# ── Rename / update ────────────────────────────────────────────────────


def test_rename_connection_via_put() -> None:
    session = _FakeSession(
        [_make_cred("bunq", connection_id="conn-1", label="Oud label")],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.put(
            f"{_CONFIGS_URL}/conn-1",
            json={"description": "Nieuw label"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["description"] == "Nieuw label"
        assert body["id"] == "conn-1"
        assert "encrypted_payload" not in resp.text


def test_update_connection_credentials_masked() -> None:
    session = _FakeSession(
        [_make_cred("bunq", connection_id="conn-1", label="Bunq")],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.put(
            f"{_CONFIGS_URL}/conn-1",
            json={"credentials": {"api_key": "new-secret-456"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_configured"] is True
        assert "new-secret-456" not in resp.text
        assert "encrypted_payload" not in resp.text


def test_update_unknown_connection_404s() -> None:
    session = _FakeSession([], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.put(
            f"{_CONFIGS_URL}/nope",
            json={"description": "X"},
        )
        assert resp.status_code == 404


# ── Delete ─────────────────────────────────────────────────────────────


def test_delete_connection() -> None:
    cred = _make_cred("bunq", connection_id="conn-1")
    session = _FakeSession([cred], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.delete(f"{_CONFIGS_URL}/conn-1")
        assert resp.status_code == 204
        # The row is gone — a subsequent lookup 404s.
        assert client.get(f"{_CONFIGS_URL}/conn-1").status_code == 404


def test_delete_unknown_connection_404s() -> None:
    session = _FakeSession([], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        assert client.delete(f"{_CONFIGS_URL}/nope").status_code == 404


# ── Pause / resume ─────────────────────────────────────────────────────


def test_pause_and_resume_connection() -> None:
    cred = _make_cred("bunq", connection_id="conn-1")
    session = _FakeSession([cred], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        paused = client.post(f"{_CONFIGS_URL}/conn-1/pause", json={})
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert cred.status == "paused"
        resumed = client.post(f"{_CONFIGS_URL}/conn-1/resume", json={})
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "active"
        assert cred.status == "active"


def test_pause_unknown_connection_404s() -> None:
    session = _FakeSession([], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(f"{_CONFIGS_URL}/nope/pause", json={})
        assert resp.status_code == 404


def test_pause_other_tenant_connection_404s() -> None:
    session = _FakeSession(
        [_make_cred("bunq", connection_id="conn-other", tenant=_OTHER_TENANT)],
        tenant=_TENANT,
    )
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(f"{_CONFIGS_URL}/conn-other/pause", json={})
        assert resp.status_code == 404


# ── Account selection ──────────────────────────────────────────────────


def test_set_connection_accounts() -> None:
    cred = _make_cred("bunq", connection_id="conn-1")
    session = _FakeSession([cred], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            f"{_CONFIGS_URL}/conn-1/accounts",
            json={"account_ids": ["acc_1", "acc_2"], "purge_unselected": False},
        )
        assert resp.status_code == 200
        assert resp.json()["selected_accounts"] == ["acc_1", "acc_2"]
        assert cred.selected_accounts == ["acc_1", "acc_2"]


def test_set_connection_accounts_does_not_purge_by_default() -> None:
    """Changing a selection never removes history without confirmation."""
    cred = _make_cred("bunq", connection_id="conn-1", selected=["acc_1"])
    session = _FakeSession([cred], tenant=_TENANT)
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            f"{_CONFIGS_URL}/conn-1/accounts",
            json={"account_ids": ["acc_2"]},
        )
        assert resp.status_code == 200
        assert resp.json()["selected_accounts"] == ["acc_2"]


# ── Connection test ────────────────────────────────────────────────────


def test_test_endpoint_sanitises_error_message(monkeypatch: Any) -> None:
    """A failing test stores and returns a redacted error, never the key."""
    cred = _make_cred("bunq", connection_id="conn-1")
    cred.encrypted_payload = b"\x00" * 16  # not decryptable → error path
    session = _FakeSession([cred], tenant=_TENANT)
    monkeypatch.setattr(cc, "_get_registry", lambda: _fake_registry())
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(f"{_CONFIGS_URL}/conn-1/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        # The failure message is sanitised/truncated (no secrets, no raw
        # traceback dump of ciphertext).
        assert len(body["message"]) <= 600


def test_test_endpoint_returns_accounts_on_success(monkeypatch: Any) -> None:
    """A successful test returns the provider's accounts for the UI."""
    cred = _encrypted_cred(
        "bunq",
        connection_id="conn-1",
        credentials={"api_key": "secret-bunq-a"},
        label="Bunq A",
    )
    session = _FakeSession([cred], tenant=_TENANT)
    monkeypatch.setattr(cc, "_get_registry", lambda: _fake_registry())
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(f"{_CONFIGS_URL}/conn-1/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert {a["id"] for a in body["accounts"]} == {"acc-a1", "acc-a2"}
        assert {a["label"] for a in body["accounts"]} == {
            "Bunq A Checking",
            "Bunq A Savings",
        }
        ibans = [a["iban"] for a in body["accounts"]]
        assert "NL01BUNQ0123456789" in ibans
        # Credentials never appear in the test response either.
        assert "secret-bunq-a" not in resp.text
        assert "encrypted_payload" not in resp.text


def test_inline_test_returns_accounts(monkeypatch: Any) -> None:
    """Unsaved credentials can be tested; accounts are offered back."""
    session = _FakeSession([], tenant=_TENANT)
    monkeypatch.setattr(cc, "_get_registry", lambda: _fake_registry())
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            "/api/v1/connectors/bunq/test",
            json={"credentials": {"api_key": "secret-bunq-a"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert {a["id"] for a in body["accounts"]} == {"acc-a1", "acc-a2"}
        assert "secret-bunq-a" not in resp.text


# ── Acceptance: two bunq connections, full lifecycle ───────────────────


def test_acceptance_two_bunq_connections_lifecycle(monkeypatch: Any) -> None:
    """Create two bunq connections, test, select, pause/resume — no leaks.

    Mirrors the story's acceptance criteria: two bunq connections in one
    tenant, each tested, different accounts selected per connection,
    pause/resume, and no credentials in any response body.
    """
    session = _FakeSession([], tenant=_TENANT)
    monkeypatch.setattr(cc, "_get_registry", lambda: _fake_registry())
    response_texts: list[str] = []

    with _client(_make_app(session, _user_ctx())) as client:
        # 1. Create two bunq connections in the same tenant.
        resp = client.post(
            _CONFIGS_URL,
            json={
                "provider_type": "bunq",
                "credentials": {"api_key": "secret-bunq-a"},
                "description": "Bunq A",
            },
        )
        assert resp.status_code == 201
        response_texts.append(resp.text)
        conn_a = resp.json()["id"]

        resp = client.post(
            _CONFIGS_URL,
            json={
                "provider_type": "bunq",
                "credentials": {"api_key": "secret-bunq-b"},
                "description": "Bunq B",
            },
        )
        assert resp.status_code == 201
        response_texts.append(resp.text)
        conn_b = resp.json()["id"]

        assert conn_a != conn_b

        # 2. Test both connections; each offers its own accounts.
        resp = client.post(f"{_CONFIGS_URL}/{conn_a}/test")
        assert resp.status_code == 200
        response_texts.append(resp.text)
        assert resp.json()["success"] is True
        assert {a["id"] for a in resp.json()["accounts"]} == {
            "acc-a1",
            "acc-a2",
        }

        resp = client.post(f"{_CONFIGS_URL}/{conn_b}/test")
        assert resp.status_code == 200
        response_texts.append(resp.text)
        assert resp.json()["success"] is True
        assert {a["id"] for a in resp.json()["accounts"]} == {"acc-b1"}

        # 3. Select different accounts per connection.
        resp = client.post(
            f"{_CONFIGS_URL}/{conn_a}/accounts",
            json={"account_ids": ["acc-a1"], "purge_unselected": False},
        )
        assert resp.status_code == 200
        response_texts.append(resp.text)
        resp = client.post(
            f"{_CONFIGS_URL}/{conn_b}/accounts",
            json={"account_ids": ["acc-b1"], "purge_unselected": False},
        )
        assert resp.status_code == 200
        response_texts.append(resp.text)

        # 4. List: both connections present with their own selections.
        resp = client.get(_CONFIGS_URL)
        assert resp.status_code == 200
        response_texts.append(resp.text)
        by_id = {c["id"]: c for c in resp.json()}
        assert set(by_id) == {conn_a, conn_b}
        assert by_id[conn_a]["selected_accounts"] == ["acc-a1"]
        assert by_id[conn_b]["selected_accounts"] == ["acc-b1"]
        assert by_id[conn_a]["last_success_at"] is not None

        # 5. Pause / resume one connection without affecting the other.
        resp = client.post(f"{_CONFIGS_URL}/{conn_a}/pause", json={})
        assert resp.status_code == 200
        response_texts.append(resp.text)
        assert resp.json()["status"] == "paused"

        resp = client.post(f"{_CONFIGS_URL}/{conn_a}/resume", json={})
        assert resp.status_code == 200
        response_texts.append(resp.text)
        assert resp.json()["status"] == "active"

        # conn_b was never touched.
        assert by_id[conn_b]["status"] == "active"

        # 6. Credentials never appear in any response body.
        joined = "\n".join(response_texts)
        assert "secret-bunq-a" not in joined
        assert "secret-bunq-b" not in joined
        assert "encrypted_payload" not in joined


def test_credential_response_stringifies_uuid_ids() -> None:
    """On PostgreSQL the ORM returns ``uuid.UUID`` objects for
    ``Credential.id``; the public response has ``str`` fields, so the
    serialiser must stringify.  Regression for the PG-only 500 that hit
    every connector-config endpoint (list/get/create/update/pause/resume/
    accounts/test) before the fix — the aiosqlite unit suite masked it
    because SQLite stores UUIDs as text."""
    cred = _make_cred("bunq", connection_id=str(uuid4()))
    cred.id = cast("Any", uuid4())  # simulate a real PostgreSQL row
    resp = cc._credential_response(cred)
    assert isinstance(resp.id, str)
    assert isinstance(resp.connection_id, str)
    assert resp.id == str(cred.id)
    assert resp.connection_id == str(cred.id)
