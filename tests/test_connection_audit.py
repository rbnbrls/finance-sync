"""Regression tests: tenant-scoped connection audit logging + redaction.

Story t_eab3b15a — tenant-scoped audit logging and credential redaction.

Covers the three acceptance criteria:

1. **Every connection-management action writes an audit entry** — create,
   update, test, pause, resume, select-accounts and delete all append a
   ``ConnectionAuditLog`` row carrying tenant, actor, action,
   connection_id and a sanitised detail payload.
2. **An admin endpoint returns those entries** —
   ``GET /api/v1/connectors/audit-log`` is tenant-scoped, newest-first,
   admin-only and supports ``connection_id`` / ``provider_key`` / ``limit``
   filters.
3. **Secrets never appear in logs, API responses or errors** — the audit
   detail payload is scrubbed at write time (defence in depth), the
   inline test path sanitises provider error messages, and captured logs
   contain no credential values.

Uses the mock-session harness style of ``test_connectors_multi_connection``
extended with query support for ``ConnectionAuditLog`` rows.
"""

# pyright: basic

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
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
from finance_sync.models import ConnectionAuditLog
from finance_sync.models.connection_audit_log import (
    AUDIT_ACCOUNTS,
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_PAUSE,
    AUDIT_RESUME,
    AUDIT_TEST,
    AUDIT_UPDATE,
)
from finance_sync.models.credential import Credential
from finance_sync.services.auth import encrypt_credential
from finance_sync.services.connection_audit import (
    list_connection_audit_events,
    log_connection_event,
)
from finance_sync.utils.redaction import REDACTED

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

_TENANT = "tenant-1"
_OTHER_TENANT = "tenant-2"
_CONFIGS_URL = "/api/v1/connectors/configs"
_AUDIT_URL = "/api/v1/connectors/audit-log"
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


def _make_audit(
    *,
    tenant: str,
    action: str,
    provider: str,
    connection_id: str | None,
    detail: dict[str, Any] | None = None,
    actor: str = "user-1",
    at: datetime | None = None,
    seq: int = 0,
) -> ConnectionAuditLog:
    """A pre-seeded audit row with a deterministic id/timestamp."""
    entry = ConnectionAuditLog(
        tenant_id=tenant,
        connection_id=connection_id,
        provider_key=provider,
        action=action,
        detail=dict(detail or {}),
        actor_user_id=actor,
        actor_role="admin",
        created_at=at
        or (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(seconds=seq)),
    )
    entry.id = f"audit-{seq}-{uuid4().hex[:8]}"
    return entry


class _FakeSession:
    """In-memory stand-in for ``AsyncSession``.

    Mirrors the WHERE semantics the API always applies: queries are
    filtered by extracting ``<column> == <value>`` constraints from the
    statement's whereclause (``tenant_id``, ``connection_id``,
    ``provider_key``, ``id``), audit listings are newest-first, and
    ``limit`` is honoured.  ``add`` generates ids and timestamps exactly
    like the ORM defaults so API-created rows are queryable afterwards.
    """

    def __init__(self, rows: list[Any], tenant: str | None = None) -> None:
        self._tenant = tenant
        self._rows = [
            r
            for r in rows
            if tenant is None or getattr(r, "tenant_id", None) == tenant
        ]
        self._added: list[Any] = []
        self._tick = 0

    # ── helpers ──────────────────────────────────────────────────────

    def _all_rows(self) -> list[Any]:
        return self._rows + list(self._added)

    @staticmethod
    def _table_constraints(stmt: Any) -> dict[str, Any]:
        """Extract ``column == value`` constraints for a table."""
        constraints: dict[str, Any] = {}
        where = getattr(stmt, "whereclause", None)
        if where is None:
            return constraints
        for node in visitors.iterate(where):
            if not isinstance(node, BinaryExpression):
                continue
            col = node.left
            key = getattr(col, "key", None)
            value = getattr(node.right, "value", None)
            if key is not None and value is not None:
                constraints[key] = value
        return constraints

    @staticmethod
    def _entity(stmt: Any) -> Any:
        try:
            descriptions = stmt.column_descriptions
        except Exception:
            return None
        if not descriptions:
            return None
        return descriptions[0].get("entity") or descriptions[0].get("type")

    @staticmethod
    def _limit(stmt: Any) -> int | None:
        clause = getattr(stmt, "_limit_clause", None)
        value = getattr(clause, "value", None)
        return int(value) if isinstance(value, int) else None

    def _query_rows(self, stmt: Any, entity: Any) -> list[Any]:
        constraints = self._table_constraints(stmt)
        rows = [r for r in self._all_rows() if isinstance(r, entity)]
        for key, value in constraints.items():
            rows = [r for r in rows if getattr(r, key, None) == value]
        if entity is ConnectionAuditLog:
            rows = sorted(
                rows,
                key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
            limit = self._limit(stmt)
            if limit is not None:
                rows = rows[:limit]
        return rows

    # ── async session surface ────────────────────────────────────────

    async def execute(self, stmt: Any) -> Any:
        entity = self._entity(stmt)
        rows = self._query_rows(stmt, entity) if entity is not None else []
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(rows)
        result.scalar_one_or_none.return_value = rows[0] if rows else None
        return result

    async def scalars(self, stmt: Any) -> Any:
        entity = self._entity(stmt)
        rows = self._query_rows(stmt, entity) if entity is not None else []
        result = MagicMock()
        result.all.return_value = list(rows)
        return result

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = f"rec-{uuid4().hex[:12]}"
        if getattr(obj, "created_at", None) is None:
            # Monotonic clock so API-created audit entries are strictly
            # ordered for the newest-first listing assertions.
            self._tick += 1
            obj.created_at = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(
                seconds=self._tick
            )
        self._added.append(obj)

    async def delete(self, obj: Any) -> None:
        if obj in self._rows:
            self._rows.remove(obj)
        if obj in self._added:
            self._added.remove(obj)

    async def flush(self) -> None:
        pass


class _FakeConnector:
    """Returns per-credential accounts so connections differ."""

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


class _UnhealthyConnector(_FakeConnector):
    """Connector whose health check fails with a secret-echoing message."""

    async def health(self) -> ConnectorHealth:
        key = str(self._config.credentials.get("api_key", ""))
        return ConnectorHealth(
            healthy=False,
            message=f"bunq rejected api key {key} (invalid credentials)",
            provider_type="bunq",
        )


def _fake_registry() -> Any:
    registry = MagicMock()
    registry.__contains__.return_value = True
    registry.get_connector.side_effect = lambda config: _FakeConnector(config)
    return registry


def _unhealthy_registry() -> Any:
    registry = MagicMock()
    registry.__contains__.return_value = True
    registry.get_connector.side_effect = lambda config: _UnhealthyConnector(
        config
    )
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


# ── Unit: log_connection_event sanitises + records metadata ────────────


class _StubSession:
    """Minimal session: records added objects, no-op flush."""

    def __init__(self) -> None:
        self.added: list[ConnectionAuditLog] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


async def test_log_event_scrubs_secret_values_from_detail() -> None:
    session: Any = _StubSession()
    entry = await log_connection_event(
        session,
        tenant_id=_TENANT,
        action=AUDIT_CREATE,
        provider_key="bunq",
        connection_id="conn-1",
        detail={
            "label": "key=abc123secret in label",
            "nested": "token ghp_1234567890abcdefgh",
            "safe": "plain label",
        },
        actor_user_id="user-1",
        actor_role="admin",
        secrets=["abc123secret"],
    )
    assert "abc123secret" not in str(entry.detail)
    assert "ghp_1234567890abcdefgh" not in str(entry.detail)
    assert entry.detail["safe"] == "plain label"
    assert entry.detail["label"] != "key=abc123secret in label"
    assert REDACTED in entry.detail["label"]


async def test_log_event_scrubs_secret_lists() -> None:
    session: Any = _StubSession()
    entry = await log_connection_event(
        session,
        tenant_id=_TENANT,
        action=AUDIT_ACCOUNTS,
        provider_key="bunq",
        connection_id="conn-1",
        detail={"selected_accounts": ["acc-1", "abc123secret"]},
        secrets=["abc123secret"],
    )
    assert "abc123secret" not in str(entry.detail)
    assert entry.detail["selected_accounts"] == ["acc-1", REDACTED]


async def test_log_event_records_tenant_actor_and_connection() -> None:
    session: Any = _StubSession()
    entry = await log_connection_event(
        session,
        tenant_id=_TENANT,
        action=AUDIT_UPDATE,
        provider_key="trading212",
        connection_id="conn-9",
        detail={"credentials_updated": True},
        actor_user_id="user-42",
        actor_role="user",
    )
    assert entry.tenant_id == _TENANT
    assert entry.connection_id == "conn-9"
    assert entry.provider_key == "trading212"
    assert entry.action == AUDIT_UPDATE
    assert entry.actor_user_id == "user-42"
    assert entry.actor_role == "user"
    assert session.added == [entry]


async def test_log_event_persists_without_flush_when_requested() -> None:
    session: Any = _StubSession()
    await log_connection_event(
        session,
        tenant_id=_TENANT,
        action=AUDIT_CREATE,
        provider_key="bunq",
        flush=False,
    )
    assert len(session.added) == 1


# ── Unit: list_connection_audit_events filters/orders ─────────────────


async def test_list_events_returns_newest_first() -> None:
    rows = [
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_CREATE,
            provider="bunq",
            connection_id="conn-1",
            at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        ),
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_DELETE,
            provider="bunq",
            connection_id="conn-1",
            at=datetime(2026, 8, 1, 11, 0, tzinfo=UTC),
        ),
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_TEST,
            provider="bunq",
            connection_id="conn-1",
            at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        ),
    ]
    session: Any = _FakeSession(rows)
    entries = await list_connection_audit_events(
        session, tenant_id=_TENANT, connection_id="conn-1"
    )
    assert [e.action for e in entries] == [
        AUDIT_TEST,
        AUDIT_DELETE,
        AUDIT_CREATE,
    ]


async def test_list_events_is_tenant_scoped() -> None:
    rows = [
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_CREATE,
            provider="bunq",
            connection_id="conn-1",
        ),
        _make_audit(
            tenant=_OTHER_TENANT,
            action=AUDIT_CREATE,
            provider="bunq",
            connection_id="conn-other",
        ),
    ]
    session: Any = _FakeSession(rows)
    entries = await list_connection_audit_events(session, tenant_id=_TENANT)
    assert len(entries) == 1
    assert entries[0].connection_id == "conn-1"


async def test_list_events_filters_by_provider_and_connection() -> None:
    rows = [
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_CREATE,
            provider="bunq",
            connection_id="conn-1",
        ),
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_CREATE,
            provider="trading212",
            connection_id="conn-2",
        ),
    ]
    session: Any = _FakeSession(rows)
    entries = await list_connection_audit_events(
        session, tenant_id=_TENANT, provider_key="bunq"
    )
    assert [e.provider_key for e in entries] == ["bunq"]


async def test_list_events_applies_limit() -> None:
    rows = [
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_TEST,
            provider="bunq",
            connection_id="conn-1",
            seq=i,
        )
        for i in range(5)
    ]
    session: Any = _FakeSession(rows)
    entries = await list_connection_audit_events(
        session, tenant_id=_TENANT, limit=2
    )
    assert len(entries) == 2


# ── API: every connection-management action writes an audit entry ──────


def test_acceptance_every_action_writes_an_audit_entry(
    monkeypatch: Any,
) -> None:
    """Full lifecycle → audit trail contains one entry per action."""
    session = _FakeSession([])
    monkeypatch.setattr(cc, "_get_registry", lambda: _fake_registry())
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            _CONFIGS_URL,
            json={
                "provider_type": "bunq",
                "credentials": {"api_key": "secret-bunq-a"},
                "description": "Bunq A",
            },
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        assert (
            client.put(
                f"{_CONFIGS_URL}/{conn_id}", json={"description": "Renamed"}
            ).status_code
            == 200
        )
        assert client.post(f"{_CONFIGS_URL}/{conn_id}/test").status_code == 200
        assert (
            client.post(f"{_CONFIGS_URL}/{conn_id}/pause", json={}).status_code
            == 200
        )
        assert (
            client.post(f"{_CONFIGS_URL}/{conn_id}/resume", json={}).status_code
            == 200
        )
        assert (
            client.post(
                f"{_CONFIGS_URL}/{conn_id}/accounts",
                json={"account_ids": ["acc-a1"]},
            ).status_code
            == 200
        )
        assert client.delete(f"{_CONFIGS_URL}/{conn_id}").status_code == 204

        entries = client.get(_AUDIT_URL).json()

    assert len(entries) == 7
    # Newest first — delete is the last action.
    assert [e["action"] for e in entries] == [
        AUDIT_DELETE,
        AUDIT_ACCOUNTS,
        AUDIT_RESUME,
        AUDIT_PAUSE,
        AUDIT_TEST,
        AUDIT_UPDATE,
        AUDIT_CREATE,
    ]
    for entry in entries:
        # tenant scoping is enforced by the query layer; the response
        # model intentionally omits tenant_id (it is implied by auth).
        assert entry["provider_key"] == "bunq"
        assert entry["connection_id"] == conn_id
        assert entry["actor_user_id"] == "user-1"
        assert entry["actor_role"] == "admin"
        assert "secret-bunq-a" not in json.dumps(entry["detail"])


def test_delete_audit_entry_survives_credential_deletion() -> None:
    """The trail outlives the connection (connection_id is retained)."""
    cred = _encrypted_cred(
        "bunq", connection_id="conn-1", credentials={"api_key": "secret-x"}
    )
    session = _FakeSession([cred])
    with _client(_make_app(session, _user_ctx())) as client:
        assert client.delete(f"{_CONFIGS_URL}/conn-1").status_code == 204
        entries = client.get(_AUDIT_URL).json()

    assert len(entries) == 1
    assert entries[0]["action"] == AUDIT_DELETE
    assert entries[0]["connection_id"] == "conn-1"
    assert client.get(f"{_CONFIGS_URL}/conn-1").status_code == 404


def test_idempotent_pause_does_not_duplicate_audit_entry() -> None:
    """Pausing an already-paused connection logs nothing."""
    cred = _make_cred("bunq", connection_id="conn-1", status="paused")
    session = _FakeSession([cred])
    with _client(_make_app(session, _user_ctx())) as client:
        client.post(f"{_CONFIGS_URL}/conn-1/pause", json={})
        client.post(f"{_CONFIGS_URL}/conn-1/pause", json={})
        entries = client.get(_AUDIT_URL).json()
    assert entries == []


# ── API: audit detail payloads never contain secrets ───────────────────


def test_audit_entry_scrubs_secret_from_label(monkeypatch: Any) -> None:
    """A label that equals the api_key is redacted inside the audit entry."""
    session = _FakeSession([])
    monkeypatch.setattr(cc, "_get_registry", lambda: _fake_registry())
    secret = "super-secret-key-123"
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            _CONFIGS_URL,
            json={
                "provider_type": "bunq",
                "credentials": {"api_key": secret},
                "description": secret,  # label == credential value
            },
        )
        assert resp.status_code == 201
        entries = client.get(_AUDIT_URL).json()

    assert len(entries) == 1
    detail = entries[0]["detail"]
    assert secret not in json.dumps(detail)
    assert detail["label"] == REDACTED
    assert detail["is_configured"] is True


def test_failed_test_audit_entry_has_no_ciphertext_or_secret(
    monkeypatch: Any,
) -> None:
    """Decrypt-failure path: raw ciphertext never reaches the audit entry."""
    cred = _make_cred("bunq", connection_id="conn-1")
    cred.encrypted_payload = b"\x00" * 32  # invalid ciphertext
    cred.nonce = b"\x00" * 12
    session = _FakeSession([cred])
    monkeypatch.setattr(cc, "_get_registry", lambda: _fake_registry())
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(f"{_CONFIGS_URL}/conn-1/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        entries = client.get(_AUDIT_URL).json()

    assert len(entries) == 1
    assert entries[0]["action"] == AUDIT_TEST
    assert entries[0]["detail"]["success"] is False
    assert (
        "0000000000000000000000000000000000000000000000000000000000000000"
        not in json.dumps(entries[0]["detail"])
    )
    assert "0000" not in json.dumps(entries[0]["detail"])


# ── API: admin endpoint is tenant-scoped, admin-only, filterable ───────


def test_audit_endpoint_requires_admin() -> None:
    session = _FakeSession([])
    with _client(_make_app(session, _user_ctx(role="user"))) as client:
        # Audit trail is admin-only…
        assert client.get(_AUDIT_URL).status_code == 403
        # …while ordinary connector reads still work for the same user.
        assert client.get(_CONFIGS_URL).status_code == 200


def test_audit_endpoint_is_tenant_scoped() -> None:
    session = _FakeSession(
        [
            _make_audit(
                tenant=_TENANT,
                action=AUDIT_CREATE,
                provider="bunq",
                connection_id="conn-1",
            ),
            _make_audit(
                tenant=_OTHER_TENANT,
                action=AUDIT_CREATE,
                provider="bunq",
                connection_id="conn-other",
            ),
        ]
    )
    with _client(_make_app(session, _user_ctx())) as client:
        entries = client.get(_AUDIT_URL).json()
    assert len(entries) == 1
    assert entries[0]["connection_id"] == "conn-1"
    assert entries[0]["actor_user_id"] == "user-1"


def test_audit_endpoint_filters_by_connection_and_provider() -> None:
    session = _FakeSession(
        [
            _make_audit(
                tenant=_TENANT,
                action=AUDIT_CREATE,
                provider="bunq",
                connection_id="conn-1",
            ),
            _make_audit(
                tenant=_TENANT,
                action=AUDIT_TEST,
                provider="trading212",
                connection_id="conn-2",
            ),
        ]
    )
    with _client(_make_app(session, _user_ctx())) as client:
        by_provider = client.get(
            _AUDIT_URL, params={"provider_key": "trading212"}
        ).json()
        by_connection = client.get(
            _AUDIT_URL, params={"connection_id": "conn-1"}
        ).json()
    assert [e["action"] for e in by_provider] == [AUDIT_TEST]
    assert [e["action"] for e in by_connection] == [AUDIT_CREATE]


def test_audit_endpoint_applies_limit() -> None:
    rows = [
        _make_audit(
            tenant=_TENANT,
            action=AUDIT_TEST,
            provider="bunq",
            connection_id="conn-1",
            seq=i,
        )
        for i in range(4)
    ]
    session: Any = _FakeSession(rows)
    with _client(_make_app(session, _user_ctx())) as client:
        entries = client.get(_AUDIT_URL, params={"limit": 2}).json()
    assert len(entries) == 2


# ── API: secrets absent from logs, responses and errors ────────────────


def test_inline_test_error_is_sanitised(monkeypatch: Any) -> None:
    """An unhealthy inline test must not echo credentials back."""
    session = _FakeSession([])
    monkeypatch.setattr(cc, "_get_registry", lambda: _unhealthy_registry())
    secret = "leaky-secret-999"
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            "/api/v1/connectors/bunq/test",
            json={"credentials": {"api_key": secret}},
        )
        assert resp.status_code == 200
        body = resp.json()
    assert body["success"] is False
    assert secret not in resp.text
    assert REDACTED in body["message"]


def test_saved_connection_test_error_is_sanitised(
    monkeypatch: Any,
) -> None:
    """The saved-config test path stores + returns a scrubbed error."""
    cred = _encrypted_cred(
        "bunq",
        connection_id="conn-1",
        credentials={"api_key": "leaky-secret-999"},
    )
    session = _FakeSession([cred])
    monkeypatch.setattr(cc, "_get_registry", lambda: _unhealthy_registry())
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(f"{_CONFIGS_URL}/conn-1/test")
        assert resp.status_code == 200
        body = resp.json()
        listed = client.get(_CONFIGS_URL).json()[0]
        entries = client.get(_AUDIT_URL).json()

    assert body["success"] is False
    assert "leaky-secret-999" not in resp.text
    # The stored connection row keeps a sanitised last_error…
    assert "leaky-secret-999" not in json.dumps(listed)
    assert REDACTED in listed["last_error"]
    # …and the audit entry does too.
    assert "leaky-secret-999" not in json.dumps(entries[0]["detail"])


def test_audit_flow_leaves_no_secrets_in_captured_logs(
    caplog: Any,
    monkeypatch: Any,
) -> None:
    """End-to-end: credential values never appear in captured log records."""
    session = _FakeSession([])
    monkeypatch.setattr(cc, "_get_registry", lambda: _unhealthy_registry())
    secret = "log-leak-sentinel-777"
    with _client(_make_app(session, _user_ctx())) as client:
        resp = client.post(
            _CONFIGS_URL,
            json={
                "provider_type": "bunq",
                "credentials": {"api_key": secret},
                "description": "Log leak test",
            },
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        client.post(f"{_CONFIGS_URL}/{conn_id}/test")
        audit = client.get(_AUDIT_URL).json()

    assert secret not in caplog.text
    # Sanity: the test actually exercised the secret through the stack.
    assert secret not in json.dumps(audit)
