"""Regression tests for connection sync database session ownership."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm.exc import DetachedInstanceError

from finance_sync.api.v1.sync import _run_connection_sync
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.models.enums import SyncRunStatus
from finance_sync.sync.orchestrator import SyncOrchestrator
from finance_sync.sync.results import SyncResult


@pytest.mark.asyncio
async def test_connection_sync_releases_request_session_before_provider_io() -> (
    None
):
    """Provider authentication must not hold the caller's DB connection."""
    db = MagicMock()
    db.close = AsyncMock()
    audit_db = MagicMock()
    audit_context = MagicMock()
    audit_context.__aenter__ = AsyncMock(return_value=audit_db)
    audit_context.__aexit__ = AsyncMock(return_value=None)
    container = SimpleNamespace(
        settings=SimpleNamespace(),
        session_factory=MagicMock(return_value=audit_context),
    )
    credential = SimpleNamespace(
        id="connection-1",
        tenant_id="tenant-1",
        provider_key="trading212",
        status="active",
        selected_accounts=[],
        encrypted_payload=b"",
        nonce=b"",
    )
    result = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        accounts_synced=1,
        transactions_synced=1,
        holdings_synced=0,
        unresolved_securities=0,
        error_message=None,
        error_category=None,
    )
    orchestrator = MagicMock()

    async def assert_session_released(
        *_args: object, **_kwargs: object
    ) -> SimpleNamespace:
        db.close.assert_awaited_once()
        return result

    orchestrator.run_sync = AsyncMock(side_effect=assert_session_released)
    latest_run_id = AsyncMock(return_value="run-1")
    record_audit = AsyncMock()

    with (
        patch(
            "finance_sync.api.v1.sync._decrypt_config",
            return_value=SimpleNamespace(
                connection_id="connection-1",
                selected_accounts=[],
            ),
        ),
        patch(
            "finance_sync.api.v1.sync.SyncOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "finance_sync.api.v1.sync._latest_run_id",
            new=latest_run_id,
        ),
        patch(
            "finance_sync.api.v1.sync._record_sync_audit",
            new=record_audit,
        ),
    ):
        link = await _run_connection_sync(
            container,
            db,
            "tenant-1",
            credential,
        )

    assert link.status == "completed"
    db.close.assert_awaited_once()
    assert latest_run_id.await_args.args[0] is audit_db
    assert record_audit.await_args.args[0] is audit_db
    assert latest_run_id.await_args.args[0] is not db
    assert record_audit.await_args.args[0] is not db


@pytest.mark.asyncio
async def test_sync_authenticates_before_acquiring_pipeline_session() -> None:
    """Provider I/O must not begin while the pipeline session owns a connection."""
    events: list[str] = []
    connector = MagicMock()

    async def authenticate() -> None:
        events.append("authenticate")

    connector.authenticate = AsyncMock(side_effect=authenticate)
    registry = MagicMock()
    registry.get_connector.return_value = connector
    registry.list_connectors.return_value = {}
    session = MagicMock()
    session_context = MagicMock()

    async def enter_session() -> MagicMock:
        assert events == ["authenticate"]
        events.append("session")
        return session

    session_context.__aenter__ = AsyncMock(side_effect=enter_session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=session_context)
    orchestrator = SyncOrchestrator(
        session_factory=session_factory,
        registry=registry,
        tenant_id="tenant-1",
    )
    orchestrator._run_pipeline = AsyncMock(
        return_value=SyncResult(
            status=SyncRunStatus.COMPLETED,
            accounts_synced=0,
            transactions_synced=0,
            holdings_synced=0,
            unresolved_securities=0,
            error_message=None,
            duration_s=0.0,
        )
    )

    await orchestrator.run_sync(
        "trading212",
        ConnectorConfig(
            provider_type="trading212",
            credentials={"api_key": "test"},
        ),
    )

    assert events == ["authenticate", "session"]


class _DetachingCredential:
    """Credential double that fails like a detached ORM row after ``close()``.

    Mirrors what SQLAlchemy does to a row once its session is closed: the
    instance survives, but every mapped attribute raises
    ``DetachedInstanceError`` instead of silently refreshing.
    """

    _DETACHED_FIELDS = frozenset(
        {
            "id",
            "tenant_id",
            "provider_key",
            "status",
            "selected_accounts",
            "encrypted_payload",
            "nonce",
        }
    )

    def __init__(self) -> None:
        self.detached = False
        self.id = "connection-1"
        self.tenant_id = "tenant-1"
        self.provider_key = "trading212"
        self.status = "active"
        self.selected_accounts: list[str] = []
        self.encrypted_payload = b"encrypted-payload"
        self.nonce = b"nonce-1"

    def __getattribute__(self, name: str) -> object:
        if name in type(self)._DETACHED_FIELDS:
            detached = object.__getattribute__(self, "detached")
            if detached:
                message = (
                    f"Instance credential.{name} is not bound to a Session; "
                    "attribute refresh operation cannot proceed"
                )
                raise DetachedInstanceError(message)
        return object.__getattribute__(self, name)


@pytest.mark.asyncio
async def test_audit_path_uses_snapshotted_credential_after_session_close() -> (
    None
):
    """The post-run audit must not read the credential's closed ORM row."""
    credential = _DetachingCredential()
    db = MagicMock()

    async def close_session() -> None:
        credential.detached = True

    db.close = AsyncMock(side_effect=close_session)
    audit_db = MagicMock()
    audit_context = MagicMock()
    audit_context.__aenter__ = AsyncMock(return_value=audit_db)
    audit_context.__aexit__ = AsyncMock(return_value=None)
    container = SimpleNamespace(
        settings=SimpleNamespace(),
        session_factory=MagicMock(return_value=audit_context),
    )
    result = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        accounts_synced=1,
        transactions_synced=2,
        holdings_synced=0,
        unresolved_securities=0,
        error_message=None,
        error_category=None,
    )
    orchestrator = MagicMock()
    orchestrator.run_sync = AsyncMock(return_value=result)
    latest_run_id = AsyncMock(return_value="run-1")
    record_audit = AsyncMock()

    with (
        patch(
            "finance_sync.api.v1.sync._decrypt_config",
            return_value=SimpleNamespace(
                connection_id="connection-1",
                selected_accounts=[],
            ),
        ),
        patch(
            "finance_sync.api.v1.sync.SyncOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "finance_sync.api.v1.sync._latest_run_id",
            new=latest_run_id,
        ),
        patch(
            "finance_sync.api.v1.sync._record_sync_audit",
            new=record_audit,
        ),
    ):
        link = await _run_connection_sync(
            container,
            db,
            "tenant-1",
            credential,
        )

    assert credential.detached is True
    assert link.status == "completed"
    assert link.connection_id == "connection-1"
    assert link.provider == "trading212"
    assert link.sync_run_id == "run-1"
    assert latest_run_id.await_args.args == (
        audit_db,
        "trading212",
        "connection-1",
    )
    audit_kwargs = record_audit.await_args.kwargs
    assert "cred" not in audit_kwargs
    assert audit_kwargs["provider_key"] == "trading212"
    assert audit_kwargs["connection_id"] == "connection-1"
    assert audit_kwargs["encrypted_payload"] == b"encrypted-payload"
    assert audit_kwargs["nonce"] == b"nonce-1"


@pytest.mark.asyncio
async def test_provider_failure_survives_audit_session_pool_timeout() -> None:
    """An exhausted pool on the audit path must not mask the provider error."""
    credential = _DetachingCredential()
    db = MagicMock()
    db.close = AsyncMock()
    container = SimpleNamespace(
        settings=SimpleNamespace(),
        session_factory=MagicMock(
            side_effect=TimeoutError(
                "QueuePool limit of size 5 overflow 0 reached"
            )
        ),
    )
    orchestrator = MagicMock()
    orchestrator.run_sync = AsyncMock(
        side_effect=PermanentError(
            "Trading212 api_key is required in credentials"
        )
    )

    with (
        patch(
            "finance_sync.api.v1.sync._decrypt_config",
            return_value=ConnectorConfig(
                provider_type="trading212",
                credentials={"api_key": "test"},
            ),
        ),
        patch(
            "finance_sync.api.v1.sync.SyncOrchestrator",
            return_value=orchestrator,
        ),
    ):
        link = await _run_connection_sync(
            container,
            db,
            "tenant-1",
            credential,
        )

    assert link.status == "error"
    assert "api_key is required" in (link.error_message or "")
    assert "QueuePool" not in (link.error_message or "")


@pytest.mark.asyncio
async def test_mark_sync_run_failed_writes_by_run_id_not_orm_instance() -> None:
    """Failed-run recovery reloads by id: no ORM row crosses the session."""
    from finance_sync.sync.sync_run import mark_sync_run_failed

    class _FakeUnitOfWork:
        instances: list[_FakeUnitOfWork] = []

        def __init__(self, session: object) -> None:
            self.session = session
            self.sync_runs = SimpleNamespace(get=AsyncMock(return_value=None))
            type(self).instances.append(self)

        async def __aenter__(self) -> _FakeUnitOfWork:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    recovery_session = MagicMock()
    recovery_session.add = MagicMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=recovery_session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session_context)
    log = MagicMock()

    with patch("finance_sync.db.uow.UnitOfWork", new=_FakeUnitOfWork):
        await mark_sync_run_failed(
            factory,
            "run-1",
            "connection failed",
            log,
            connection_id="connection-1",
            connector="trading212",
            error_category="provider",
        )

    assert _FakeUnitOfWork.instances[0].sync_runs.get.await_args.args == (
        "run-1",
    )
    stored = recovery_session.add.call_args.args[0]
    assert stored.connection_id == "connection-1"
    assert stored.connector == "trading212"

    # A run that never reached ``start_sync_run`` is logged, not written.
    skipped_factory = MagicMock()
    await mark_sync_run_failed(
        skipped_factory,
        None,
        "failed before the run existed",
        log,
    )
    skipped_factory.assert_not_called()
    assert log.error.call_args.args[0] == "sync_failed_before_run_created"
