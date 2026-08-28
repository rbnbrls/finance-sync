"""Focused tests for small but previously uncovered API and worker paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.api.deps.auth import AuthContext
from finance_sync.api.v1 import file_uploads as file_uploads_api
from finance_sync.api.v1.file_uploads import (
    _csv_mapping,
    _detect,
    _inspect_path,
    _normalise,
    list_file_upload_runs,
)
from finance_sync.api.v1.market_data import _live_quote, _parse_options
from finance_sync.api.v1.webhooks import (
    CreateWebhookRequest,
    _get_service,
    create_webhook,
    delete_webhook,
    get_webhook,
    list_webhooks,
)
from finance_sync.models.credential import Credential
from finance_sync.services.account_selection import (
    filter_account_ids,
    filter_accounts,
    load_account_selection,
)
from finance_sync.services.performance import (
    AttributionResponse,
    BenchmarkComparisonResponse,
    MWRResponse,
    PerformanceService,
    TWRResponse,
)
from finance_sync.services.read.portfolio import PortfolioReadService
from finance_sync.services.webhook import (
    WebhookService,
    _SlidingWindowCounter,
    validate_webhook_url,
)
from finance_sync.sync.stages.holdings import HoldingsSyncStage
from finance_sync.worker.health import WorkerHealthServer
from finance_sync.worker.jobs import (
    _file_sha256,
    _move_batch,
    _record_watch_failure,
    retryable_job,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Order-ID / Datum", "orderiddatum"), (None, ""), ("ÄBC", "bc")],
)
def test_upload_normalise(value: object, expected: str) -> None:
    assert _normalise(value) == expected


def test_upload_detects_csv_and_broker_markers(tmp_path: Path) -> None:
    path = tmp_path / "DEGIRO_portfolio.csv"
    path.write_text("Order ID;Value date;Local value\n1;2025-01-01;10\n")

    markers, evidence = _inspect_path(path)

    assert {"degiro_filename", "degiro_content"} <= markers
    assert "DEGIRO-kolommen gevonden" in evidence
    assert _detect(markers) == (
        "degiro_pension",
        "DEGIRO-kenmerken gevonden",
    )


def test_upload_detects_manual_json_and_unknown_file(tmp_path: Path) -> None:
    manual = tmp_path / "expenses.json"
    manual.write_text(json.dumps({"expenses": [{"amount": 4}]}))
    unknown = tmp_path / "readme.bin"
    unknown.write_bytes(b"no broker data")

    markers, _ = _inspect_path(manual)
    assert _detect(markers)[0] == "manual_expense"
    unknown_markers, evidence = _inspect_path(unknown)
    assert _detect(unknown_markers)[0] is None
    assert evidence == []


@pytest.mark.asyncio
async def test_file_upload_history_projects_provider_from_join() -> None:
    """The shared history endpoint returns the joined provider key."""
    run = SimpleNamespace(
        id="run-1",
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        file_names=["positions.xlsx"],
        created_count=2,
        updated_count=1,
        safe_error=None,
        rows_total=0,
        skipped_count=0,
        rejected_count=0,
        warnings=[],
        period_start=None,
        period_end=None,
        attempt=1,
        status="completed",
    )
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(
            all=MagicMock(
                return_value=[(run, "saxo_investor", '{"_label":"Mijn Saxo"}')]
            )
        )
    )

    result = await list_file_upload_runs(
        auth=SimpleNamespace(tenant_id="tenant-1"),
        db=db,
    )

    assert [item.model_dump() for item in result] == [
        {
            "id": "run-1",
            "created_at": run.created_at,
            "file_names": ["positions.xlsx"],
            "status": "completed",
            "created_count": 2,
            "updated_count": 1,
            "provider_type": "saxo_investor",
            "profile_name": "Mijn Saxo",
            "period_start": None,
            "period_end": None,
            "rows_total": 0,
            "skipped_count": 0,
            "rejected_count": 0,
            "warnings": [],
            "error": None,
            "attempt": 1,
            "retryable": False,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "target"),
    [
        ("degiro_pension", "preview_degiro_import"),
        ("saxo_investor", "import_saxo_files"),
        ("csv_import", "import_generic_file"),
        ("manual_expense", "import_generic_file"),
    ],
)
async def test_file_upload_dispatch_routes_each_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str, target: str
) -> None:
    """The public upload contract delegates to the correct adapter."""
    adapter = AsyncMock(return_value={"provider": provider})
    monkeypatch.setattr(file_uploads_api, target, adapter)

    result = await file_uploads_api.dispatch_file_import(
        request=SimpleNamespace(),
        provider_type=provider,
        connection_id="connection-1",
        files=[],
        auth=SimpleNamespace(tenant_id="tenant-1"),
        db=MagicMock(),
    )

    assert result == {"provider": provider}
    adapter.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_upload_dispatch_rejects_unknown_provider() -> None:
    with pytest.raises(file_uploads_api.HTTPException) as exc_info:
        await file_uploads_api.dispatch_file_import(
            request=SimpleNamespace(),
            provider_type="unknown",
            connection_id="connection-1",
            files=[],
            auth=SimpleNamespace(tenant_id="tenant-1"),
            db=MagicMock(),
        )

    assert exc_info.value.status_code == 422


class _AsyncContext:
    def __init__(self, value: object = None) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_worker_process_shutdown_without_components() -> None:
    from finance_sync.worker import WorkerProcess

    process = WorkerProcess(settings=MagicMock())

    await process._shutdown()

    assert process._scheduler is None
    assert process._running_tasks == set()


@pytest.mark.asyncio
async def test_worker_process_shutdown_drains_and_stops_components() -> None:
    from finance_sync.worker import WorkerProcess

    scheduler = MagicMock()
    scheduler.running_jobs.return_value = ["sync"]
    scheduler.wait_for_completion = AsyncMock()
    scheduler.stop = AsyncMock()
    health = MagicMock()
    health.stop = AsyncMock()
    process = WorkerProcess(settings=MagicMock())
    process._scheduler = scheduler
    process._health_server = health
    process._running_tasks.add(asyncio.create_task(asyncio.sleep(60)))

    await process._shutdown()

    scheduler.pause.assert_called_once_with()
    health.stop.assert_awaited_once_with()
    scheduler.wait_for_completion.assert_awaited_once_with()
    scheduler.stop.assert_awaited_once_with()
    assert process._running_tasks
    assert all(task.cancelled() for task in process._running_tasks)


@pytest.mark.asyncio
async def test_worker_process_shutdown_logs_drain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import WorkerProcess

    scheduler = MagicMock()
    scheduler.running_jobs.return_value = ["sync"]
    scheduler.stop = AsyncMock()
    process = WorkerProcess(settings=MagicMock())
    process._scheduler = scheduler

    async def timeout(
        awaitable: object, _timeout: float = 0.0, **_kwargs: object
    ) -> None:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise TimeoutError

    monkeypatch.setattr("finance_sync.worker.asyncio.wait_for", timeout)

    await process._shutdown()

    scheduler.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_worker_process_start_registers_signals_and_shutdowns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_sync.worker as worker_module

    settings = MagicMock()
    settings.environment.value = "dev"
    settings.worker_health_port = 9999
    process = worker_module.WorkerProcess(settings=settings)
    signal_calls: list[tuple[signal.Signals, object]] = []

    class FakeLoop:
        def add_signal_handler(
            self, sig: signal.Signals, callback: object
        ) -> None:
            signal_calls.append((sig, callback))

    class FakeContainer:
        def dispose(self) -> _AsyncContext:
            return _AsyncContext()

    class FakeScheduler:
        def __init__(self, **_kwargs: object) -> None:
            self.stop = AsyncMock()

        async def start(self) -> None:
            return None

        def job_summary(self) -> list[str]:
            return ["sync"]

        def pause(self) -> None:
            return None

        def running_jobs(self) -> list[str]:
            return []

    class FakeHealth:
        def __init__(self, **_kwargs: object) -> None:
            self.stop = AsyncMock()

        async def serve(self) -> None:
            process._shutdown_event.set()

    monkeypatch.setattr(
        worker_module.Container,
        "from_settings",
        lambda _settings: FakeContainer(),
    )
    monkeypatch.setattr(worker_module, "WorkerScheduler", FakeScheduler)
    monkeypatch.setattr(worker_module, "WorkerHealthServer", FakeHealth)
    monkeypatch.setattr(
        worker_module.asyncio, "get_running_loop", lambda: FakeLoop()
    )

    await process.start()

    assert {sig for sig, _callback in signal_calls} == {
        signal.SIGTERM,
        signal.SIGINT,
    }
    assert process._scheduler is not None
    assert process._health_server is not None


def test_worker_signal_handler_sets_shutdown_event() -> None:
    import finance_sync.worker as worker_module

    process = worker_module.WorkerProcess(settings=MagicMock())

    process._signal_handler(signal.SIGTERM)

    assert process._shutdown_event.is_set()


def test_run_worker_configures_runtime_and_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_sync.worker as worker_module

    settings = MagicMock()
    settings.is_production = False
    settings.log_level = "INFO"
    worker = MagicMock()
    monkeypatch.setattr(
        worker_module, "WorkerProcess", lambda _settings: worker
    )
    logging = MagicMock()
    glitchtip = MagicMock()
    monkeypatch.setattr(worker_module, "configure_logging", logging)
    monkeypatch.setattr(worker_module, "configure_glitchtip", glitchtip)

    def interrupt(coro: object) -> None:
        coro.close()  # type: ignore[attr-defined]
        raise KeyboardInterrupt

    monkeypatch.setattr(worker_module.asyncio, "run", interrupt)

    worker_module.run_worker(settings)

    logging.assert_called_once_with(json_output=False, log_level="INFO")
    glitchtip.assert_called_once_with(settings)


def test_worker_main_delegates_to_run_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_sync.worker as worker_module

    run = MagicMock()
    monkeypatch.setattr(worker_module, "run_worker", run)

    worker_module.main()

    run.assert_called_once_with()


def test_upload_csv_mapping_supports_dutch_headers(tmp_path: Path) -> None:
    path = tmp_path / "transactions.csv"
    path.write_text("Datum;Omschrijving;Bedrag\n2025-01-01;Koffie;-3.50\n")
    assert _csv_mapping(path) == {
        "date": "Datum",
        "description": "Omschrijving",
        "amount": "Bedrag",
    }


def test_market_data_options_omit_internal_label() -> None:
    credential = Credential(description='{"_label":"hidden","region":"eu"}')
    assert _parse_options(credential) == {"region": "eu"}


def test_market_data_options_invalid_json_is_empty() -> None:
    credential = Credential(description="not-json")
    assert _parse_options(credential) == {}


def _mcp_result(payload: object) -> SimpleNamespace:
    """Create the small response double used by MCP wrapper tests."""
    return SimpleNamespace(model_dump=lambda: payload)


def _mcp_context() -> SimpleNamespace:
    """Return a context-shaped object for direct MCP handler calls."""
    return SimpleNamespace()


@pytest.mark.asyncio
async def test_mcp_resources_serialize_read_service_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.mcp import server

    session = SimpleNamespace(aclose=AsyncMock())
    accounts = _mcp_result({"items": [{"id": "a1", "name": "Bank"}]})
    portfolio = _mcp_result({"accounts": [], "total_value": "0"})
    net_worth = _mcp_result({"net_worth": "12.34"})
    tx = SimpleNamespace(
        id="tx-1",
        model_dump=lambda: {"occurred_at": "2025-01-01", "amount": "3"},
    )
    account = SimpleNamespace(id="a1", name="Bank", account_type="checking")
    service = SimpleNamespace(
        _session=session,
        list_accounts=AsyncMock(
            return_value=SimpleNamespace(
                items=[account], model_dump=accounts.model_dump
            )
        ),
        list_account_transactions=AsyncMock(
            return_value=SimpleNamespace(items=[tx])
        ),
        get_portfolio=AsyncMock(return_value=portfolio),
        get_net_worth=AsyncMock(return_value=net_worth),
    )
    monkeypatch.setattr(server, "_get_tenant_id", lambda _ctx: "tenant-1")
    monkeypatch.setattr(
        server, "_get_read_service", AsyncMock(return_value=service)
    )
    ctx = _mcp_context()

    assert (
        json.loads(await server.resource_accounts(ctx))["items"][0]["id"]
        == "a1"
    )
    assert (
        json.loads(await server.resource_portfolio(ctx))["total_value"] == "0"
    )
    assert (
        json.loads(await server.resource_net_worth(ctx))["net_worth"] == "12.34"
    )
    transactions = json.loads(await server.resource_transactions(ctx))
    assert transactions[0]["account_name"] == "Bank"
    assert transactions[0]["account_type"] == "checking"
    assert session.aclose.await_count == 4


@pytest.mark.asyncio
async def test_mcp_read_tools_forward_filters_and_close_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.mcp import server

    session = SimpleNamespace(aclose=AsyncMock())
    service = SimpleNamespace(
        _session=session,
        get_cashflow=AsyncMock(return_value=_mcp_result({"net": "4"})),
        list_sync_runs=AsyncMock(return_value=_mcp_result({"items": []})),
    )
    monkeypatch.setattr(server, "_get_tenant_id", lambda _ctx: "tenant-1")
    monkeypatch.setattr(
        server, "_get_read_service", AsyncMock(return_value=service)
    )
    ctx = _mcp_context()

    assert json.loads(await server.tool_get_cashflow(ctx, "7d"))["net"] == "4"
    assert json.loads(
        await server.tool_list_sync_runs(ctx, 3, "bunq", "failed")
    ) == {"items": []}
    service.get_cashflow.assert_awaited_once()
    assert service.get_cashflow.await_args.args[0] == "tenant-1"
    service.list_sync_runs.assert_awaited_once_with(
        "tenant-1", limit=3, connector="bunq", status="failed"
    )
    assert session.aclose.await_count == 2


@pytest.mark.asyncio
async def test_mcp_holding_tools_handle_success_not_found_and_invalid_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.mcp import server

    session = SimpleNamespace(aclose=AsyncMock())
    uow = SimpleNamespace(session=session, commit=AsyncMock())
    holding = SimpleNamespace(
        _uow=uow,
        feed=AsyncMock(return_value={"clusters": []}),
        calendar=AsyncMock(return_value=[]),
        set_ack=AsyncMock(side_effect=[False, True]),
        correct=AsyncMock(return_value=True),
        get_notification_preference=AsyncMock(return_value={"enabled": False}),
        set_notification_preference=AsyncMock(
            return_value={"enabled": True, "event_types": ["news"]}
        ),
    )
    monkeypatch.setattr(
        server, "_get_holding_relevance_service", lambda _ctx: holding
    )
    monkeypatch.setattr(
        server, "_auth_principal", lambda _ctx: ("tenant-1", "principal-1")
    )
    ctx = _mcp_context()

    feed = json.loads(
        await server.tool_get_holding_feed(
            ctx, date_from="not-a-date", date_to="2025-01-01T00:00:00+00:00"
        )
    )
    assert feed == {"clusters": []}
    assert holding.feed.await_args.kwargs["date_from"] is None
    assert holding.feed.await_args.kwargs["date_to"] is not None
    assert (
        json.loads(await server.tool_get_holding_calendar(ctx, date_from="bad"))
        == []
    )
    assert (
        json.loads(
            await server.tool_acknowledge_holding_cluster(ctx, "cluster-1")
        )["status"]
        == "not_found"
    )
    assert (
        json.loads(
            await server.tool_acknowledge_holding_cluster(
                ctx, "cluster-2", False
            )
        )["acknowledged"]
        is False
    )
    assert (
        json.loads(
            await server.tool_correct_holding_item(
                ctx, "item-1", reason="wrong"
            )
        )["status"]
        == "corrected"
    )
    assert (
        json.loads(await server.tool_get_holding_notification_preferences(ctx))[
            "enabled"
        ]
        is False
    )
    assert (
        json.loads(
            await server.tool_set_holding_notification_preferences(
                ctx, enabled=True, event_types=["news"]
            )
        )["enabled"]
        is True
    )
    assert uow.commit.await_count == 2
    assert session.aclose.await_count == 7


@pytest.mark.asyncio
async def test_market_data_live_quote_returns_matching_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = SimpleNamespace(
        id="connection-1",
        encrypted_payload=b"payload",
        nonce=b"nonce",
        description='{"_label":"demo"}',
    )
    connector = MagicMock()
    connector.authenticate = AsyncMock()
    connector.fetch_portfolio = AsyncMock(
        return_value=[
            {"ticker": "AAPL", "currentPrice": "123.45", "currencyCode": "USD"}
        ]
    )
    connector.close = AsyncMock()
    registry = MagicMock()
    registry.return_value.get_connector.return_value = connector
    monkeypatch.setattr(
        "finance_sync.api.v1.market_data._credentials",
        AsyncMock(return_value=[credential]),
    )
    monkeypatch.setattr(
        "finance_sync.api.v1.market_data.decrypt_credential",
        lambda *_args: '{"api_key":"secret"}',
    )
    monkeypatch.setattr(
        "finance_sync.api.v1.market_data.ConnectorRegistry",
        registry,
    )
    monkeypatch.setattr(
        "finance_sync.api.v1.market_data._security",
        AsyncMock(return_value=None),
    )

    result = await _live_quote(
        MagicMock(),
        MagicMock(),
        _auth(),
        "aapl",
        None,
    )

    assert result["symbol"] == "AAPL"
    assert result["price"] == 123.45
    assert result["source"] == "trading212"
    connector.authenticate.assert_awaited_once()
    connector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_market_data_live_quote_without_connection_raises_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(
        "finance_sync.api.v1.market_data._credentials",
        AsyncMock(return_value=[]),
    )
    with pytest.raises(HTTPException) as error:
        await _live_quote(MagicMock(), MagicMock(), _auth(), "AAPL", None)
    assert error.value.status_code == 404


class _Scheduler:
    def __init__(self, running: bool) -> None:
        self.running = running

    def is_running(self) -> bool:
        return self.running

    def job_summary(self) -> list[dict[str, object]]:
        return [{"id": "sync", "running": self.running}]


@pytest.mark.asyncio
async def test_worker_health_handlers_cover_scheduler_and_monitor() -> None:
    monitor = MagicMock()
    monitor.summarize.return_value = [{"job_id": "sync"}]
    server = WorkerHealthServer(
        monitor=monitor,
        scheduler=_Scheduler(True),  # type: ignore[arg-type]
    )

    health = await server._handle_health(None)
    ready = await server._handle_ready(None)
    jobs = await server._handle_jobs(None)
    live = await server._handle_live(None)

    health_data = json.loads(health.body._value.decode())  # type: ignore[attr-defined]
    assert health_data == {
        "status": "ok",
        "uptime": health_data["uptime"],
        "scheduler": {
            "running": True,
            "jobs": [{"id": "sync", "running": True}],
        },
    }
    assert json.loads(ready.body._value.decode())["status"] == "ok"  # type: ignore[attr-defined]
    assert json.loads(jobs.body._value.decode()) == {
        "jobs": [{"job_id": "sync"}]
    }  # type: ignore[attr-defined]
    assert json.loads(live.body._value.decode()) == {"status": "ok"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_worker_health_reports_degraded_without_scheduler() -> None:
    server = WorkerHealthServer()
    health = await server._handle_health(None)
    ready = await server._handle_ready(None)
    jobs = await server._handle_jobs(None)

    assert json.loads(health.body._value.decode())["status"] == "degraded"  # type: ignore[attr-defined]
    assert json.loads(ready.body._value.decode()) == {  # type: ignore[attr-defined]
        "status": "not_ready",
        "scheduler_running": False,
    }
    assert json.loads(jobs.body._value.decode()) == {"jobs": []}  # type: ignore[attr-defined]


def test_worker_file_helpers(tmp_path: Path) -> None:
    first = tmp_path / "first.CSV"
    missing = tmp_path / "missing.csv"
    first.write_bytes(b"hello")
    destination = tmp_path / "quarantine"

    _move_batch([first, missing], destination)

    moved = list(destination.iterdir())
    assert len(moved) == 1
    assert moved[0].suffix == ".csv"
    assert _file_sha256(moved[0]) == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


@pytest.mark.asyncio
async def test_worker_records_watch_failure() -> None:
    session = MagicMock()
    session.flush = AsyncMock()
    files = [Path("a.csv"), Path("b.csv")]

    await _record_watch_failure(
        session,
        run_id="run-1",
        tenant_id="tenant-1",
        connection_id="connection-1",
        digest="digest",
        hashes=["a", "b"],
        files=files,
        attempt=2,
    )

    session.add.assert_called_once()
    recorded = session.add.call_args.args[0]
    assert recorded.status == "quarantined"
    assert recorded.file_names == ["a.csv", "b.csv"]
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_retryable_job_decorator_retries_and_preserves_result() -> None:
    calls = 0

    @retryable_job(max_attempts=2, base_delay=0)
    async def job() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = "temporary"
            raise RuntimeError(message)
        return "done"

    assert await job() == "done"
    assert calls == 2


def test_webhook_service_is_cached_on_request() -> None:
    settings = SimpleNamespace(redis_url=None)
    container = SimpleNamespace(settings=settings, session_factory=MagicMock())
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "finance_sync.dependencies.get_container",
            lambda _request: container,
        )
        first = _get_service(request)  # type: ignore[arg-type]
        second = _get_service(request)  # type: ignore[arg-type]

    assert first is second


def _webhook(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "wh-1",
        "url": "https://example.test/hook",
        "events": ["sync.completed"],
        "is_active": True,
        "description": "test",
        "secret": "secret-value",
        "rate_limit_max_per_minute": 60,
        "created_at": None,
        "updated_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _auth() -> AuthContext:
    return AuthContext(
        user=SimpleNamespace(
            tenant_id="tenant-1",
            id="user-1",
            roles=[],
        ),
    )


@pytest.mark.asyncio
async def test_webhook_create_list_get_delete_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace()
    service = MagicMock()
    service.create_webhook = AsyncMock(return_value=_webhook())
    service.list_webhooks = AsyncMock(return_value=[_webhook()])
    service.get_webhook = AsyncMock(return_value=_webhook())
    service.delete_webhook = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "finance_sync.api.v1.webhooks._get_service",
        lambda _request: service,
    )
    body = CreateWebhookRequest(
        url="https://example.test/hook",
        events=["sync.completed"],
        description="test",
    )

    created = await create_webhook(request, body, _auth())
    listed = await list_webhooks(request, _auth(), event_type="sync.completed")
    fetched = await get_webhook(request, "wh-1", _auth())
    deleted = await delete_webhook(request, "wh-1", _auth())

    assert created["id"] == "wh-1"
    assert listed["total"] == 1
    assert fetched["url"] == "https://example.test/hook"
    assert deleted is None
    service.list_webhooks.assert_awaited_once_with(
        tenant_id="tenant-1", event_type="sync.completed"
    )


@pytest.mark.asyncio
async def test_webhook_get_and_delete_missing_raise_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace()
    service = MagicMock()
    service.get_webhook = AsyncMock(return_value=None)
    service.delete_webhook = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "finance_sync.api.v1.webhooks._get_service",
        lambda _request: service,
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as get_error:
        await get_webhook(request, "missing", _auth())
    with pytest.raises(HTTPException) as delete_error:
        await delete_webhook(request, "missing", _auth())
    assert get_error.value.status_code == 404
    assert delete_error.value.status_code == 404


@pytest.mark.asyncio
async def test_account_selection_loads_and_filters_connections() -> None:
    credentials = [
        SimpleNamespace(id="conn-a", selected_accounts=["acc-1"]),
        SimpleNamespace(id="conn-b", selected_accounts=[]),
    ]
    session = MagicMock()
    session.scalars = AsyncMock(return_value=credentials)

    selection = await load_account_selection(session, "tenant-1")
    assert selection == {"conn-a": {"acc-1"}, "conn-b": None}

    accounts = [
        SimpleNamespace(
            id="1", connection_id="conn-a", external_account_id="acc-1"
        ),
        SimpleNamespace(
            id="2", connection_id="conn-a", external_account_id="acc-2"
        ),
        SimpleNamespace(
            id="3", connection_id="conn-b", external_account_id="acc-3"
        ),
    ]
    assert await filter_accounts(session, "tenant-1", accounts) == [
        accounts[0],
        accounts[2],
    ]


@pytest.mark.asyncio
async def test_account_selection_empty_inputs_short_circuit() -> None:
    session = MagicMock()
    assert await filter_accounts(session, "tenant-1", []) == []
    assert await filter_account_ids(session, "tenant-1", []) == []
    session.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_holdings_stage_persists_resolved_and_tracks_unresolved() -> None:
    writer = MagicMock()
    writer.resolve_security_reference = AsyncMock(
        side_effect=[
            (SimpleNamespace(id="security-1"), None),
            (None, "ISIN:UNKNOWN"),
            (None, None),
        ]
    )
    writer.persist_holding = AsyncMock()
    stage = HoldingsSyncStage(writer)
    holdings = [
        SimpleNamespace(security_reference=SimpleNamespace(symbol="A")),
        SimpleNamespace(security_reference=SimpleNamespace(symbol="B")),
        SimpleNamespace(security_reference=SimpleNamespace(symbol="C")),
    ]

    result = await stage.run(
        MagicMock(), holdings, account_id="account-1", provider_key="demo"
    )

    assert result.count == 1
    assert result.unresolved_keys == frozenset({"ISIN:UNKNOWN"})
    writer.persist_holding.assert_awaited_once()
    assert writer.persist_holding.await_args.args[2:] == (
        "account-1",
        "security-1",
    )


@pytest.mark.asyncio
async def test_portfolio_read_returns_empty_shapes() -> None:
    first_result = MagicMock()
    first_result.scalars.return_value.all.return_value = []
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    execute = AsyncMock(side_effect=[first_result, first_result, count_result])
    session = MagicMock()
    session.execute = execute
    service = PortfolioReadService(session)

    portfolio = await service.get_portfolio("tenant-1")
    holdings = await service.get_holdings("tenant-1")

    assert portfolio.total_value == 0
    assert portfolio.accounts == []
    assert holdings.items == []
    assert holdings.total == 0


@pytest.mark.asyncio
async def test_portfolio_read_builds_values_and_latest_holding_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = datetime(2025, 1, 2, tzinfo=UTC)
    holding = SimpleNamespace(
        account_id="account-1",
        security_id="security-1",
        observed_at=observed,
        quantity=Decimal(2),
        cost_basis=Decimal(100),
        cost_basis_currency="EUR",
        market_value=None,
        currency_code="EUR",
        price=Decimal(60),
        price_currency="EUR",
    )
    account = SimpleNamespace(
        id="account-1", name="Broker", account_type="brokerage"
    )
    security = SimpleNamespace(
        id="security-1", ticker="ABC", name="ABC Corp", security_type="stock"
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [holding]
    account_result = MagicMock()
    account_result.scalars.return_value.all.return_value = [account]
    security_result = MagicMock()
    security_result.scalars.return_value.all.return_value = [security]
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            result,
            account_result,
            security_result,
            result,
            count_result,
            account_result,
            security_result,
        ]
    )
    monkeypatch.setattr(
        "finance_sync.services.read.portfolio.fetch_latest_daily_prices",
        AsyncMock(return_value={}),
    )
    service = PortfolioReadService(session)

    portfolio = await service.get_portfolio("tenant-1")
    holdings = await service.get_holdings("tenant-1", account_id="account-1")

    assert portfolio.total_value == Decimal(120)
    assert portfolio.total_cost_basis == Decimal(100)
    assert portfolio.accounts[0].account_name == "Broker"
    assert portfolio.accounts[0].holdings[0].unrealised_pl == Decimal(20)
    assert portfolio.accounts[0].holdings[0].unrealised_pl_pct == Decimal(20)
    assert holdings.total == 1
    assert holdings.items[0].ticker == "ABC"
    assert holdings.items[0].market_value == Decimal(120)
    assert holdings.items[0].unrealised_pl_pct == Decimal(20)
    assert holdings.meta.as_of == observed


@pytest.mark.asyncio
async def test_schedule_runner_helpers_and_tick_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import schedule_runner

    naive = datetime(2025, 1, 1)
    assert schedule_runner._ensure_aware(naive).tzinfo is UTC
    assert schedule_runner._ensure_aware(None) is None
    aware = datetime(2025, 1, 1, tzinfo=UTC)
    assert schedule_runner._ensure_aware(aware) is aware

    failure = RuntimeError("tick failed")
    monkeypatch.setattr(
        schedule_runner,
        "run_due_schedules",
        AsyncMock(side_effect=failure),
    )
    with pytest.raises(RuntimeError, match="tick failed"):
        await schedule_runner.run_scheduled_syncs_job(SimpleNamespace())


@pytest.mark.asyncio
async def test_worker_enrich_prices_selects_identifiers_and_counts_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    class Session:
        info: dict[str, object] = {}

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    securities = [
        SimpleNamespace(id="s1", ticker="ABC", figi=None, isin=None),
        SimpleNamespace(id="s2", ticker=None, figi="FIGI-2", isin=None),
        SimpleNamespace(id="s3", ticker=None, figi=None, isin="ISIN-3"),
        SimpleNamespace(id="s4", ticker=None, figi=None, isin=None),
    ]
    uow = SimpleNamespace(
        securities=SimpleNamespace(list=AsyncMock(return_value=securities))
    )
    monkeypatch.setattr("finance_sync.db.uow.UnitOfWork", lambda _session: uow)
    gateway = SimpleNamespace(
        get_latest_quote=AsyncMock(
            side_effect=[{"price": 1}, None, RuntimeError("provider down")]
        )
    )
    container = SimpleNamespace(
        enrichment_gateway=gateway,
        session_factory=lambda: Session(),
    )

    result = await jobs.enrich_prices_job(container)

    assert result == {"enriched": 1, "failed": 2}
    assert [
        call.kwargs["identifier_type"]
        for call in gateway.get_latest_quote.await_args_list
    ] == [
        "ticker",
        "figi",
        "isin",
    ]


@pytest.mark.asyncio
async def test_worker_export_job_skips_when_disabled_or_unconfigured() -> None:
    from finance_sync.worker.jobs import export_wealthfolio_job

    disabled = SimpleNamespace(
        settings=SimpleNamespace(
            worker_job_export_enabled=False,
            wealthfolio_server_url=None,
            wealthfolio_password=None,
        )
    )
    assert (await export_wealthfolio_job(disabled))["status"] == "skipped"

    unconfigured = SimpleNamespace(
        settings=SimpleNamespace(
            worker_job_export_enabled=True,
            wealthfolio_server_url=None,
            wealthfolio_password=None,
        )
    )
    result = await export_wealthfolio_job(unconfigured)
    assert result["status"] == "skipped"
    assert "not set" in result["reason"]


@pytest.mark.asyncio
async def test_worker_webhook_and_outbox_jobs_close_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    service = SimpleNamespace(
        retry_due_deliveries=AsyncMock(return_value=3), close=AsyncMock()
    )
    service_factory = MagicMock(return_value=service)
    monkeypatch.setattr(
        "finance_sync.services.webhook.WebhookService", service_factory
    )
    settings = SimpleNamespace(redis_url=None)
    container = SimpleNamespace(
        session_factory=MagicMock(), settings=settings, redis_client=None
    )
    result = await jobs.process_webhook_retries_job(container)
    assert result == {"retried": 3}
    service.close.assert_awaited_once()

    publisher = SimpleNamespace(
        register_handler=MagicMock(), run_once=AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        "finance_sync.worker.jobs.OutboxPublisher", lambda **_kwargs: publisher
    )
    outbox_service = SimpleNamespace(
        handle_outbox_message=MagicMock(), close=AsyncMock()
    )
    monkeypatch.setattr(
        "finance_sync.services.webhook.WebhookService",
        lambda **_kwargs: outbox_service,
    )
    assert await jobs.process_outbox_job(container) == {"processed": 2}
    publisher.register_handler.assert_called_once()
    outbox_service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_webhook_and_outbox_jobs_propagate_failures_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    settings = SimpleNamespace(redis_url=None)
    container = SimpleNamespace(
        session_factory=MagicMock(), settings=settings, redis_client=None
    )
    retry_service = SimpleNamespace(
        retry_due_deliveries=AsyncMock(
            side_effect=RuntimeError("retry failed")
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        "finance_sync.services.webhook.WebhookService",
        MagicMock(return_value=retry_service),
    )
    with pytest.raises(RuntimeError, match="retry failed"):
        await jobs.process_webhook_retries_job(container)
    retry_service.close.assert_awaited_once()

    publisher = SimpleNamespace(
        register_handler=MagicMock(),
        run_once=AsyncMock(side_effect=RuntimeError("outbox failed")),
    )
    monkeypatch.setattr(jobs, "OutboxPublisher", lambda **_kwargs: publisher)
    outbox_service = SimpleNamespace(
        handle_outbox_message=MagicMock(), close=AsyncMock()
    )
    monkeypatch.setattr(
        "finance_sync.services.webhook.WebhookService",
        lambda **_kwargs: outbox_service,
    )
    with pytest.raises(RuntimeError, match="outbox failed"):
        await jobs.process_outbox_job(container)
    outbox_service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_holding_relevance_job_isolates_tenant_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    session = SimpleNamespace()
    tenants = [
        SimpleNamespace(id="tenant-ok"),
        SimpleNamespace(id="tenant-bad"),
    ]
    commits = 0
    rollbacks = 0

    class Uow:
        def __init__(self, _session: object) -> None:
            self.tenants = SimpleNamespace(list=AsyncMock(return_value=tenants))

        async def commit(self) -> None:
            nonlocal commits
            commits += 1

        async def rollback(self) -> None:
            nonlocal rollbacks
            rollbacks += 1

    class Service:
        def __init__(self, _uow: object, explainer: object) -> None:
            self.explainer = explainer

        async def build_feed(self, tenant_id: str) -> dict[str, str]:
            if tenant_id == "tenant-bad":
                message = "feed failed"
                raise RuntimeError(message)
            return {"tenant": tenant_id}

        async def dispatch_new_cluster_notifications(
            self, _tenant_id: str
        ) -> int:
            return 1

    monkeypatch.setattr("finance_sync.db.uow.UnitOfWork", Uow)
    monkeypatch.setattr(
        "finance_sync.services.holding_relevance.HoldingRelevanceService",
        Service,
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(hermes_explanation_enabled=False),
        session_factory=lambda: _AsyncContext(session),
    )

    result = await jobs.holding_relevance_build_job(container)

    assert result["status"] == "completed"
    assert result["tenants"] == 2
    assert commits == 1
    assert rollbacks == 1
    assert any(item.get("error") == "feed failed" for item in result["results"])


@pytest.mark.asyncio
async def test_export_job_returns_completed_for_configured_empty_tenant_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    session = SimpleNamespace(info={})
    monkeypatch.setattr(
        jobs,
        "UnitOfWork",
        lambda _session: SimpleNamespace(
            tenants=SimpleNamespace(list=AsyncMock(return_value=[]))
        ),
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(
            worker_job_export_enabled=True,
            wealthfolio_server_url="https://wealthfolio.test",
            wealthfolio_password="secret",
        ),
        session_factory=lambda: _AsyncContext(session),
    )

    result = await jobs.export_wealthfolio_job(container)

    assert result == {"status": "completed", "tenants": 0, "results": []}


@pytest.mark.asyncio
async def test_intel_refresh_job_delegates_to_intel_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    delegated = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(
        "finance_sync.intel.scheduler.intel_refresh_job", delegated
    )
    container = SimpleNamespace()

    result = await jobs.intel_refresh_job(container)

    assert result == {"status": "ok"}
    delegated.assert_awaited_once_with(container)


@pytest.mark.asyncio
async def test_holding_relevance_job_uses_hermes_explainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    session = SimpleNamespace()
    tenant = SimpleNamespace(id="tenant-hermes")
    commits = 0

    class Uow:
        def __init__(self, _session: object) -> None:
            self.tenants = SimpleNamespace(
                list=AsyncMock(return_value=[tenant])
            )

        async def commit(self) -> None:
            nonlocal commits
            commits += 1

        async def rollback(self) -> None:
            message = "rollback should not be called"
            raise AssertionError(message)

    class Service:
        def __init__(self, _uow: object, explainer: object) -> None:
            assert explainer == "hermes"

        async def build_feed(self, _tenant_id: str) -> dict[str, bool]:
            return {"built": True}

        async def dispatch_new_cluster_notifications(
            self, _tenant_id: str
        ) -> int:
            return 0

    monkeypatch.setattr("finance_sync.db.uow.UnitOfWork", Uow)
    monkeypatch.setattr(
        "finance_sync.services.hermes_relevance.build_hermes_explainer",
        MagicMock(return_value="hermes"),
    )
    monkeypatch.setattr(
        "finance_sync.services.holding_relevance.HoldingRelevanceService",
        Service,
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(hermes_explanation_enabled=True),
        session_factory=lambda: _AsyncContext(session),
    )

    result = await jobs.holding_relevance_build_job(container)

    assert result["tenants"] == 1
    assert commits == 1


@pytest.mark.asyncio
async def test_schedule_runner_export_skip_branches() -> None:
    from finance_sync.worker.schedule_runner import run_export

    settings = SimpleNamespace(
        worker_job_export_enabled=True,
        wealthfolio_server_url=None,
        wealthfolio_password=None,
    )

    unknown = SimpleNamespace(target_id="unknown", tenant_id="tenant-1")
    container = SimpleNamespace(settings=settings)
    assert (await run_export(container, schedule=unknown))["reason"] == (
        "unknown_exporter"
    )

    class Session:
        def __init__(self, target: object) -> None:
            self.target = target

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _stmt: object) -> object:
            return self.target

    def container_for(target: object) -> SimpleNamespace:
        return SimpleNamespace(
            settings=settings,
            session_factory=lambda: Session(target),
        )

    inactive = SimpleNamespace(status="disabled")
    schedule = SimpleNamespace(
        target_id="firefly:target-1", tenant_id="tenant-1"
    )
    assert (await run_export(container_for(inactive), schedule=schedule))[
        "reason"
    ] == ("target_inactive")

    unconfigured = SimpleNamespace(
        status="active", encrypted_secret=None, secret_nonce=None
    )
    assert (await run_export(container_for(unconfigured), schedule=schedule))[
        "reason"
    ] == ("target_unconfigured")

    no_target = SimpleNamespace(target_id="firefly:", tenant_id="tenant-1")
    assert (await run_export(container_for(None), schedule=no_target))[
        "reason"
    ] == ("target_inactive")

    legacy = SimpleNamespace(target_id="wealthfolio", tenant_id="tenant-1")
    disabled_settings = SimpleNamespace(
        worker_job_export_enabled=False,
        wealthfolio_server_url=None,
        wealthfolio_password=None,
    )
    disabled_container = SimpleNamespace(settings=disabled_settings)
    assert (await run_export(disabled_container, schedule=legacy))[
        "reason"
    ] == ("global_gate_disabled")


@pytest.mark.asyncio
async def test_schedule_runner_ingestion_skips_dangling_connection() -> None:
    from finance_sync.worker.schedule_runner import _run_ingestion

    class Session:
        info: dict[str, object] = {}

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _stmt: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    settings = SimpleNamespace(
        worker_job_bunq_sync_enabled=True,
        worker_job_trading212_sync_enabled=True,
    )
    container = SimpleNamespace(
        settings=settings,
        session_factory=lambda: Session(),
    )
    schedule = SimpleNamespace(
        id="schedule-1", target_id="missing-connection", tenant_id="tenant-1"
    )

    assert await _run_ingestion(container, schedule=schedule) == {
        "status": "skipped",
        "reason": "connection_missing",
    }


@pytest.mark.asyncio
async def test_webhook_attempt_delivery_success_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.models.enums import WebhookDeliveryStatus
    from finance_sync.services import webhook as webhook_module

    settings = SimpleNamespace(
        webhook_request_timeout_s=2.0,
        webhook_max_retries=3,
        webhook_retry_base_delay_s=1.0,
    )
    service = WebhookService(MagicMock(), settings)
    service._is_rate_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        webhook_module, "validate_webhook_url", lambda *_args, **_kwargs: None
    )
    response = SimpleNamespace(status_code=204, text="")
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    service._http_client = client  # type: ignore[assignment]
    session = SimpleNamespace(execute=AsyncMock())
    webhook = SimpleNamespace(id="wh-1", url="https://example.test/hook")
    log_entry = SimpleNamespace(
        id="log-1",
        webhook_id="wh-1",
        payload={"signature": "sig"},
        status=WebhookDeliveryStatus.PENDING,
        attempt_number=1,
        max_attempts=3,
        next_retry_at=None,
        response_status_code=None,
        response_body=None,
        duration_ms=None,
        error_message=None,
    )

    await service._attempt_delivery(log_entry, webhook, session)

    assert log_entry.status == WebhookDeliveryStatus.DELIVERED
    assert log_entry.response_status_code == 204
    assert log_entry.error_message is None
    client.post.assert_awaited_once()

    timeout_log = SimpleNamespace(**log_entry.__dict__)
    timeout_log.status = WebhookDeliveryStatus.PENDING
    timeout_log.attempt_number = 1
    timeout_log.next_retry_at = None
    timeout_log.error_message = None
    import httpx

    client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    await service._attempt_delivery(timeout_log, webhook, session)
    assert timeout_log.status == WebhookDeliveryStatus.FAILED
    assert timeout_log.response_status_code == 0
    assert timeout_log.next_retry_at is not None


@pytest.mark.asyncio
async def test_webhook_attempt_delivery_rate_limited_and_retry_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.models.enums import WebhookDeliveryStatus

    settings = SimpleNamespace(
        webhook_request_timeout_s=2.0,
        webhook_max_retries=3,
        webhook_retry_base_delay_s=1.0,
    )
    service = WebhookService(MagicMock(), settings)
    service._is_rate_allowed = AsyncMock(return_value=False)  # type: ignore[method-assign]
    session = SimpleNamespace(execute=AsyncMock())
    webhook = SimpleNamespace(id="wh-1", url="https://example.test/hook")
    log_entry = SimpleNamespace(
        id="log-1",
        payload={},
        status=WebhookDeliveryStatus.PENDING,
        attempt_number=1,
        max_attempts=3,
        next_retry_at=None,
        response_status_code=None,
        response_body=None,
        duration_ms=None,
        error_message=None,
    )
    await service._attempt_delivery(log_entry, webhook, session)
    assert log_entry.status == WebhookDeliveryStatus.RATE_LIMITED
    assert log_entry.response_status_code == 429
    assert log_entry.next_retry_at is None

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _stmt: object) -> SimpleNamespace:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))

    empty_service = WebhookService(lambda: Session(), settings)
    assert await empty_service.retry_due_deliveries() == 0


@pytest.mark.asyncio
async def test_degiro_watchfolder_processes_preview_and_archives_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.config.settings import Settings
    from finance_sync.worker import jobs

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "portfolio.csv"
    source.write_text("Datum;Omschrijving;Bedrag\n2025-01-01;Test;1\n")
    os.utime(source, (1, 1))
    settings = Settings(
        debug=False,
        database_url=None,
        redis_url=None,
        degiro_watch_stable_seconds=1,
        degiro_import_staging_directory=tmp_path,
    )
    tenant = SimpleNamespace(id="tenant-1")
    credential = SimpleNamespace(id="connection-1", status="active")
    config = SimpleNamespace(
        options={"watchfolder": str(incoming), "account_key": "demo"}
    )
    connection = {
        "tenant": tenant,
        "credential": credential,
        "config": config,
    }

    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one_or_none(self) -> object:
            return self.value

    class Session:
        def __init__(self) -> None:
            self.info: dict[str, object] = {}
            self.added: list[object] = []
            self.execute_calls = 0

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _stmt: object) -> Result:
            self.execute_calls += 1
            return Result(None)

        def add(self, item: object) -> None:
            self.added.append(item)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    session = Session()
    monkeypatch.setattr(
        jobs,
        "_get_tenant_connections",
        AsyncMock(return_value=[connection]),
    )
    monkeypatch.setattr(jobs, "validate_local_files", lambda *_args: None)
    monkeypatch.setattr(
        jobs,
        "build_preview",
        AsyncMock(
            return_value={
                "report_types": ["portfolio"],
                "period_start": None,
                "period_end": None,
                "rows": 1,
                "skipped": 0,
                "warnings": [],
            }
        ),
    )
    monkeypatch.setattr(jobs, "execute_run", AsyncMock())
    container = SimpleNamespace(
        settings=settings, session_factory=lambda: session
    )

    result = await jobs.process_degiro_watchfolders_job(container)

    assert result == {"processed": 1, "quarantined": 0, "duplicates": 0}
    assert not source.exists()
    assert list((incoming / "archive").rglob("*.csv"))


@pytest.mark.asyncio
async def test_degiro_watchfolder_quarantines_invalid_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.config.settings import Settings
    from finance_sync.worker import jobs

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "broken.csv"
    source.write_text("bad")
    os.utime(source, (1, 1))
    settings = Settings(
        debug=False,
        database_url=None,
        redis_url=None,
        degiro_watch_stable_seconds=1,
        degiro_import_staging_directory=tmp_path,
    )
    connection = {
        "tenant": SimpleNamespace(id="tenant-1"),
        "credential": SimpleNamespace(id="connection-1", status="active"),
        "config": SimpleNamespace(options={"watchfolder": str(incoming)}),
    }

    class Session:
        info: dict[str, object] = {}

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _stmt: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, _item: object) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        jobs, "_get_tenant_connections", AsyncMock(return_value=[connection])
    )
    monkeypatch.setattr(
        jobs,
        "validate_local_files",
        MagicMock(side_effect=ValueError("invalid file")),
    )
    session = Session()
    container = SimpleNamespace(
        settings=settings, session_factory=lambda: session
    )

    result = await jobs.process_degiro_watchfolders_job(container)

    assert result == {"processed": 0, "quarantined": 1, "duplicates": 0}
    assert not source.exists()
    assert list((incoming / "quarantine").rglob("*.csv"))


@pytest.mark.asyncio
async def test_schedule_runner_actual_budget_legacy_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import schedule_runner

    settings = SimpleNamespace(worker_job_export_enabled=True)
    result = SimpleNamespace(status="completed", error_message=None)
    exporter = SimpleNamespace(run_export=AsyncMock(return_value=result))
    config = SimpleNamespace(from_settings=MagicMock(return_value="config"))
    exporter_type = SimpleNamespace(
        ActualBudgetExporter=MagicMock(return_value=exporter)
    )
    monkeypatch.setattr(
        "finance_sync.exporter.actual_budget.config.ActualBudgetConfig", config
    )
    monkeypatch.setattr(
        "finance_sync.exporter.actual_budget.exporter.ActualBudgetExporter",
        exporter_type.ActualBudgetExporter,
    )
    container = SimpleNamespace(settings=settings, session_factory=MagicMock())
    schedule = SimpleNamespace(target_id="actual-budget", tenant_id="tenant-1")

    outcome = await schedule_runner.run_export(container, schedule=schedule)

    assert outcome == {"status": "completed", "error": None}
    config.from_settings.assert_called_once_with(settings)
    exporter.run_export.assert_awaited_once_with(account_ids=None)


@pytest.mark.asyncio
async def test_schedule_runner_firefly_target_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import schedule_runner

    target = SimpleNamespace(
        id="target-1",
        status="active",
        encrypted_secret=b"secret",
        secret_nonce=b"nonce",
        configuration={"server_url": "https://firefly.test"},
        selected_account_ids=["account-1"],
    )

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _stmt: object) -> object:
            return target

    result = SimpleNamespace(status="completed", error_message=None)
    exporter = SimpleNamespace(run_export=AsyncMock(return_value=result))
    exporter_class = MagicMock(return_value=exporter)
    config_class = MagicMock()
    monkeypatch.setattr(
        "finance_sync.exporter.firefly.config.FireflyConfig", config_class
    )
    monkeypatch.setattr(
        "finance_sync.exporter.firefly.exporter.FireflyExporter", exporter_class
    )
    monkeypatch.setattr(
        schedule_runner,
        "decrypt_credential",
        lambda *_args: '{"access_token":"token"}',
    )
    settings = SimpleNamespace()
    container = SimpleNamespace(
        settings=settings,
        session_factory=lambda: Session(),
    )
    schedule = SimpleNamespace(
        target_id="firefly:target-1", tenant_id="tenant-1"
    )

    outcome = await schedule_runner.run_export(container, schedule=schedule)

    assert outcome == {"status": "completed", "error": None}
    exporter.run_export.assert_awaited_once_with(account_ids=["account-1"])


@pytest.mark.asyncio
async def test_schedule_runner_ghostfolio_investbrain_and_securo_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import schedule_runner

    target = SimpleNamespace(
        id="target-1",
        status="active",
        encrypted_secret=b"secret",
        secret_nonce=b"nonce",
        configuration={"server_url": "https://destination.test"},
        selected_account_ids=["account-1"],
    )

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _stmt: object) -> object:
            return target

    monkeypatch.setattr(
        schedule_runner,
        "decrypt_credential",
        lambda *_args: '{"access_token":"token","password":"secret"}',
    )
    settings = SimpleNamespace()
    container = SimpleNamespace(
        settings=settings, session_factory=lambda: Session()
    )

    class ClientContext:
        async def __aenter__(self) -> ClientContext:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    ghost_client = ClientContext()
    ghost_exporter = SimpleNamespace(
        run_export=AsyncMock(
            return_value={"status": "completed", "failures": []}
        )
    )
    monkeypatch.setattr(
        "finance_sync.exporter.ghostfolio.client.GhostfolioClient",
        MagicMock(return_value=ghost_client),
    )
    monkeypatch.setattr(
        "finance_sync.exporter.ghostfolio.exporter.GhostfolioExporter",
        MagicMock(return_value=ghost_exporter),
    )
    ghost = await schedule_runner.run_export(
        container,
        schedule=SimpleNamespace(
            target_id="ghostfolio:target-1", tenant_id="tenant-1"
        ),
    )
    assert ghost == {"status": "completed", "error": None}
    ghost_exporter.run_export.assert_awaited_once()

    invest_client = ClientContext()
    invest_exporter = SimpleNamespace(
        run_export=AsyncMock(
            return_value={"status": "failed", "failures": ["bad row"]}
        )
    )
    monkeypatch.setattr(
        "finance_sync.exporter.investbrain.client.InvestBrainClient",
        MagicMock(return_value=invest_client),
    )
    monkeypatch.setattr(
        "finance_sync.exporter.investbrain.exporter.InvestBrainExporter",
        MagicMock(return_value=invest_exporter),
    )
    invest = await schedule_runner.run_export(
        container,
        schedule=SimpleNamespace(
            target_id="investbrain:target-1", tenant_id="tenant-1"
        ),
    )
    assert invest == {"status": "failed", "error": "bad row"}

    securo_result = SimpleNamespace(status="completed", error_message=None)
    securo_exporter = SimpleNamespace(
        run_export=AsyncMock(return_value=securo_result)
    )
    monkeypatch.setattr(
        "finance_sync.exporter.securo.exporter.SecuroExporter",
        MagicMock(return_value=securo_exporter),
    )
    securo = await schedule_runner.run_export(
        container,
        schedule=SimpleNamespace(
            target_id="securo:target-1", tenant_id="tenant-1"
        ),
    )
    assert securo == {"status": "completed", "error": None}
    securo_exporter.run_export.assert_awaited_once_with(
        account_ids=["account-1"], push=True
    )


def test_sync_schedule_helpers_cover_fallback_and_redaction() -> None:
    from finance_sync.models.sync_schedule import SyncSchedule
    from finance_sync.services.sync_schedule import (
        _default_schedule_payload,
        _sanitise_audit_detail,
        compute_next_run,
        describe_schedule,
        resolve_tenant_timezone,
    )

    assert (
        resolve_tenant_timezone("not/a-zone", fallback="also/invalid") == "UTC"
    )
    assert resolve_tenant_timezone(None) == "Europe/Amsterdam"
    assert _default_schedule_payload() == {
        "frequency": "weekdays",
        "time": "07:00",
        "weekdays": [0, 1, 2, 3, 4],
    }
    detail = _sanitise_audit_detail(
        {"token": "api_key=secret", "nested": ["password=hunter2"]}
    )
    assert "secret" not in str(detail)

    row = SyncSchedule(
        tenant_id="tenant-1",
        scope="ingestion",
        target_id="connection-1",
        enabled=True,
        schedule=_default_schedule_payload(),
        schema_version=1,
        timezone="Europe/Amsterdam",
        version=1,
    )
    assert compute_next_run(row, count=1)
    row.enabled = False
    assert compute_next_run(row) is None
    assert compute_next_run({"frequency": "invalid", "timezone": "UTC"}) is None
    assert "Elke werkdag" in describe_schedule(row)


@pytest.mark.asyncio
async def test_sync_schedule_service_get_missing_and_noop_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.models.sync_schedule import SyncSchedule
    from finance_sync.services.sync_schedule import (
        ScheduleNotFoundError,
        SyncScheduleService,
    )

    row = SyncSchedule(
        id="schedule-1",
        tenant_id="tenant-1",
        scope="ingestion",
        target_id="connection-1",
        enabled=True,
        schedule={"frequency": "daily", "time": "07:00"},
        schema_version=1,
        timezone="UTC",
        version=3,
    )

    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one_or_none(self) -> object:
            return self.value

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(None), Result(row)]),
        flush=AsyncMock(),
        add=MagicMock(),
    )
    service = SyncScheduleService(session)
    with pytest.raises(ScheduleNotFoundError):
        await service.get_for_tenant("tenant-1", "missing")
    assert await service.update("tenant-1", "schedule-1") is row
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_schedule_service_lists_filters_and_updates_with_audit() -> (
    None
):
    from finance_sync.models.sync_schedule import SyncSchedule
    from finance_sync.services.sync_schedule import SyncScheduleService

    row = SyncSchedule(
        id="schedule-2",
        tenant_id="tenant-1",
        scope="ingestion",
        target_id="connection-1",
        enabled=True,
        schedule={"frequency": "daily", "time": "07:00"},
        schema_version=1,
        timezone="UTC",
        version=3,
    )

    class ScalarRows:
        def all(self) -> list[SyncSchedule]:
            return [row]

    session = SimpleNamespace(
        scalars=AsyncMock(return_value=ScalarRows()),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: row)
        ),
        flush=AsyncMock(),
        add=MagicMock(),
    )
    service = SyncScheduleService(session)
    service._audit = AsyncMock()  # type: ignore[method-assign]

    assert await service.list_for_tenant("tenant-1", scope="ingestion") == [row]
    updated = await service.update(
        "tenant-1",
        "schedule-2",
        schedule={"frequency": "daily", "time": "08:00"},
        timezone="Europe/Amsterdam",
        enabled=False,
        version=3,
        actor_api_key_id="key-1",
    )

    assert updated.enabled is False
    assert updated.timezone == "Europe/Amsterdam"
    assert updated.version == 4
    assert updated.next_run_at is None
    session.flush.assert_awaited_once()
    service._audit.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sync_schedule_service_rejects_stale_version() -> None:
    from finance_sync.models.sync_schedule import SyncSchedule
    from finance_sync.services.sync_schedule import (
        ScheduleConflictError,
        SyncScheduleService,
    )

    row = SyncSchedule(
        id="schedule-3",
        tenant_id="tenant-1",
        scope="ingestion",
        target_id="connection-1",
        enabled=True,
        schedule={"frequency": "daily", "time": "07:00"},
        schema_version=1,
        timezone="UTC",
        version=4,
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: row)
        )
    )

    with pytest.raises(ScheduleConflictError):
        await SyncScheduleService(session).update(
            "tenant-1", "schedule-3", version=3
        )


@pytest.mark.asyncio
async def test_sync_schedule_service_ensure_returns_existing_and_race_winner() -> (
    None
):
    from finance_sync.models.sync_schedule import SyncSchedule
    from finance_sync.services.sync_schedule import SyncScheduleService

    existing = SyncSchedule(
        id="existing",
        tenant_id="tenant-1",
        scope="ingestion",
        target_id="connection-1",
        enabled=True,
        schedule={"frequency": "daily", "time": "07:00"},
        schema_version=1,
        timezone="UTC",
        version=1,
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: existing),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    service = SyncScheduleService(session)
    assert (
        await service.ensure_for_scope(
            "tenant-1", scope="ingestion", target_id="connection-1"
        )
        is existing
    )
    session.add.assert_not_called()

    winner = SimpleNamespace(id="winner")
    race_session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: None),
                SimpleNamespace(scalar_one_or_none=lambda: winner),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(
            side_effect=__import__("sqlalchemy").exc.IntegrityError(
                "insert", {}, Exception("duplicate")
            )
        ),
        rollback=AsyncMock(),
    )
    assert (
        await SyncScheduleService(race_session).ensure_for_scope(
            "tenant-1", scope="ingestion", target_id="connection-1"
        )
        is winner
    )
    race_session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_schedule_audit_truncates_large_details() -> None:
    from finance_sync.models.sync_schedule import SyncSchedule
    from finance_sync.services.sync_schedule import SyncScheduleService

    row = SyncSchedule(
        tenant_id="tenant-1",
        scope="ingestion",
        target_id="x" * 2500,
        enabled=True,
        schedule={"frequency": "daily", "time": "07:00"},
        schema_version=1,
        timezone="UTC",
        version=1,
    )
    session = SimpleNamespace(add=MagicMock(), flush=AsyncMock())

    await SyncScheduleService(session)._audit(
        tenant_id="tenant-1",
        schedule=row,
        action="schedule.update",
        old={"schedule": row.schedule, "timezone": "UTC"},
        new={"schedule": row.schedule, "timezone": "UTC"},
        changed_fields={"detail": list(range(1000))},
        actor_user_id=None,
        actor_role=None,
        actor_api_key_id=None,
    )

    assert session.add.call_args.args[0].detail == {
        "truncated": True,
        "scope": "ingestion",
    }


@pytest.mark.asyncio
async def test_worker_connection_loader_skips_failed_decryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    tenant = SimpleNamespace(id="tenant-1")
    broken = SimpleNamespace(
        id="credential-1",
        encrypted_payload="ciphertext",
        nonce="nonce",
        selected_accounts=None,
        description=None,
    )
    plain = SimpleNamespace(
        id="credential-2",
        encrypted_payload=None,
        nonce=None,
        selected_accounts=["account-1"],
        description='{"_label":"Bunq"}',
    )
    session = SimpleNamespace(
        info={"settings": MagicMock()},
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [broken, plain])
            )
        ),
    )
    uow = SimpleNamespace(
        tenants=SimpleNamespace(list=AsyncMock(return_value=[tenant])),
        session=session,
    )
    monkeypatch.setattr(
        "finance_sync.services.auth.decrypt_credential",
        MagicMock(side_effect=ValueError("invalid credential")),
    )

    result = await jobs._get_tenant_connections(uow, "bunq")

    assert [item["credential"].id for item in result] == ["credential-2"]
    assert result[0]["config"].credentials == {}
    assert result[0]["config"].selected_accounts == ["account-1"]


@pytest.mark.asyncio
async def test_worker_connection_loader_decrypts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    tenant = SimpleNamespace(id="tenant-1")
    credential = SimpleNamespace(
        id="credential-1",
        encrypted_payload="ciphertext",
        nonce="nonce",
        selected_accounts=None,
        description=None,
    )
    session = SimpleNamespace(
        info={"settings": MagicMock()},
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [credential])
            )
        ),
    )
    uow = SimpleNamespace(
        tenants=SimpleNamespace(list=AsyncMock(return_value=[tenant])),
        session=session,
    )
    monkeypatch.setattr(
        "finance_sync.services.auth.decrypt_credential",
        MagicMock(return_value='{"api_key":"secret", "region":"eu"}'),
    )

    result = await jobs._get_tenant_connections(uow, "bunq")

    assert result[0]["config"].credentials == {
        "api_key": "secret",
        "region": "eu",
    }
    assert result[0]["secrets"] == ["secret", "eu"]


@pytest.mark.asyncio
async def test_sync_connector_job_handles_empty_and_mixed_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.models.enums import SyncRunStatus
    from finance_sync.sync.results import SyncResult
    from finance_sync.worker import jobs

    settings = SimpleNamespace(
        worker_retry_max_attempts=1,
        worker_retry_base_delay_s=0.0,
    )
    session = SimpleNamespace(info={})
    container = SimpleNamespace(
        settings=settings,
        session_factory=lambda: _AsyncContext(session),
    )
    monkeypatch.setattr(
        jobs, "_get_tenant_connections", AsyncMock(return_value=[])
    )
    empty = await jobs.sync_connector_job(container, "bunq")
    assert empty == {"provider": "bunq", "connections_synced": 0, "results": []}

    connections = [
        {
            "tenant": SimpleNamespace(id="tenant-ok"),
            "credential": SimpleNamespace(
                id="connection-ok", status="active", selected_accounts=[]
            ),
            "config": SimpleNamespace(provider_type="bunq"),
        },
        {
            "tenant": SimpleNamespace(id="tenant-bad"),
            "credential": SimpleNamespace(
                id="connection-bad", status="active", selected_accounts=[]
            ),
            "config": SimpleNamespace(provider_type="bunq"),
        },
    ]
    monkeypatch.setattr(
        jobs, "_get_tenant_connections", AsyncMock(return_value=connections)
    )

    class Orchestrator:
        def __init__(self, **kwargs: object) -> None:
            self.tenant_id = kwargs["tenant_id"]

        async def run_sync(self, **_kwargs: object) -> SyncResult:
            if self.tenant_id == "tenant-bad":
                message = "sync failed"
                raise RuntimeError(message)
            return SyncResult(
                status=SyncRunStatus.COMPLETED,
                accounts_synced=1,
                transactions_synced=2,
                holdings_synced=3,
                unresolved_securities=1,
                error_message=None,
                duration_s=0.25,
            )

    monkeypatch.setattr(jobs, "SyncOrchestrator", Orchestrator)
    result = await jobs.sync_connector_job(container, "bunq")

    assert result["connections_synced"] == 2
    assert result["total_accounts"] == 1
    assert result["total_transactions"] == 2
    assert result["total_holdings"] == 3
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_bunq_job_delegates_to_connector_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    delegated = AsyncMock(return_value={"provider": "bunq"})
    monkeypatch.setattr(jobs, "sync_connector_job", delegated)
    container = SimpleNamespace()

    assert await jobs.sync_bunq_job(container) == {"provider": "bunq"}
    delegated.assert_awaited_once_with(container, "bunq")


@pytest.mark.asyncio
async def test_transaction_stage_resolves_security_and_tracks_unresolved() -> (
    None
):
    from datetime import datetime

    from finance_sync.connectors.models import (
        CanonicalTransactionData,
        SecurityReference,
    )
    from finance_sync.sync.stages.transactions import TransactionSyncStage

    resolved = SimpleNamespace(id="security-1")
    writer = SimpleNamespace(
        resolve_security_reference=AsyncMock(
            side_effect=[(resolved, None), (None, "UNKNOWN")]
        ),
        persist_transaction=AsyncMock(),
    )
    transactions = [
        CanonicalTransactionData(
            provider_key="bunq",
            external_transaction_id="tx-1",
            external_account_id="account-1",
            amount=Decimal("1.00"),
            occurred_at=datetime.now(UTC),
            transaction_type="deposit",
            security_reference=SecurityReference(isin="NL0001"),
        ),
        CanonicalTransactionData(
            provider_key="bunq",
            external_transaction_id="tx-2",
            external_account_id="account-1",
            amount=Decimal("-2.00"),
            occurred_at=datetime.now(UTC),
            transaction_type="fee",
            security_reference=SecurityReference(name="Unknown asset"),
        ),
        CanonicalTransactionData(
            provider_key="bunq",
            external_transaction_id="tx-3",
            external_account_id="account-1",
            amount=Decimal("3.00"),
            occurred_at=datetime.now(UTC),
            transaction_type="deposit",
        ),
    ]

    result = await TransactionSyncStage(writer).run(
        MagicMock(),
        transactions,
        account_id="account-1",
        provider_type="bunq",
        connection_id="connection-1",
    )

    assert result.count == 3
    assert result.unresolved_keys == frozenset({"UNKNOWN"})
    assert writer.resolve_security_reference.await_count == 2
    assert writer.persist_transaction.await_count == 3
    writer.persist_transaction.assert_any_await(
        writer.persist_transaction.await_args_list[0].args[0],
        transactions[0],
        "account-1",
        security_id="security-1",
        connection_id="connection-1",
    )


@pytest.mark.asyncio
async def test_sync_cursor_read_and_upsert_support_connection_scopes() -> None:
    from finance_sync.models import SyncCursor
    from finance_sync.sync.sync_cursor import (
        get_connector_cursors,
        get_cursor,
        upsert_sync_cursor,
    )

    timestamp = datetime.now(UTC)
    scoped = SyncCursor(
        tenant_id="tenant-1",
        connector="bunq",
        connection_id="connection-1",
        resource="account-1",
        cursor=timestamp,
    )
    legacy = SyncCursor(
        tenant_id="tenant-1",
        connector="bunq",
        connection_id=None,
        resource="legacy",
        cursor=timestamp,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[scoped, legacy]),
        scalar=AsyncMock(side_effect=[None, scoped]),
        add=MagicMock(),
        flush=AsyncMock(),
    )

    result = await get_connector_cursors(
        session,
        tenant_id="tenant-1",
        connector="bunq",
        connection_id="connection-1",
    )
    assert result == {"account-1": timestamp, "legacy": timestamp}
    assert (
        await get_cursor(
            session,
            tenant_id="tenant-1",
            connector="bunq",
            resource="missing",
        )
        is None
    )

    created = await upsert_sync_cursor(
        session,
        tenant_id="tenant-1",
        connector="bunq",
        resource="new",
        cursor=timestamp,
        connection_id="connection-1",
    )
    updated = await upsert_sync_cursor(
        session,
        tenant_id="tenant-1",
        connector="bunq",
        resource="account-1",
        cursor=timestamp,
        connection_id="connection-1",
    )
    assert created.resource == "new"
    assert updated is scoped
    session.add.assert_called_once()
    assert session.flush.await_count == 2


@pytest.mark.asyncio
async def test_account_persistence_resolves_connection_owner_on_create() -> (
    None
):
    from finance_sync.connectors.models import CanonicalAccountData
    from finance_sync.sync.persistence import AccountPersistence

    account = CanonicalAccountData(
        provider_key="bunq",
        external_account_id="external-account",
        name="Current account",
        account_type="checking",
        currency_code="EUR",
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(owner_user_id="user-1")),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    uow = SimpleNamespace(
        session=session,
        accounts=SimpleNamespace(
            get_by_external_id=AsyncMock(return_value=None)
        ),
    )

    result = await AccountPersistence("tenant-1").persist_account(
        uow, account, connection_id="connection-1"
    )

    assert result.owner_user_id == "user-1"
    session.get.assert_awaited_once()
    assert session.flush.await_count == 1


@pytest.mark.asyncio
async def test_holding_persistence_defaults_unknown_source_on_create() -> None:
    from finance_sync.connectors.models import (
        CanonicalHoldingData,
        SecurityReference,
    )
    from finance_sync.models.enums import HoldingSource
    from finance_sync.sync.persistence import HoldingPersistence

    observed = datetime(2025, 1, 1, tzinfo=UTC)
    holding = CanonicalHoldingData(
        provider_key="broker",
        external_account_id="external-account",
        observed_at=observed,
        quantity=2,
        security_reference=SecurityReference(isin="IE00TEST"),
        source="legacy-source",
        market_value=200,
        currency_code="EUR",
    )
    session = SimpleNamespace(add=MagicMock(), flush=AsyncMock())
    uow = SimpleNamespace(
        session=session,
        holdings=SimpleNamespace(get_by_snapshot=AsyncMock(return_value=None)),
    )

    result = await HoldingPersistence("tenant-1").persist_holding(
        uow, holding, "account-1", "security-1"
    )

    assert result.source == HoldingSource.PROVIDER_SYNC
    assert result.market_value == 200
    assert session.flush.await_count == 1


@pytest.mark.asyncio
async def test_transaction_persistence_updates_and_normalises_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.connectors.models import CanonicalTransactionData
    from finance_sync.sync import persistence
    from finance_sync.sync.persistence import TransactionPersistence

    transaction = CanonicalTransactionData(
        provider_key="bunq",
        external_transaction_id="tx-1",
        external_account_id="account-1",
        amount=Decimal("12.50"),
        occurred_at=datetime.now(UTC),
        transaction_type="new-provider-type",
        description="Updated description",
        status="new-status",
    )
    existing = SimpleNamespace(
        id="transaction-1",
        revision=1,
        amount=Decimal("1.00"),
        currency_code="EUR",
        occurred_at=transaction.occurred_at,
        booked_at=None,
        transaction_type="deposit",
        description="Old description",
        quantity=None,
        unit_price=None,
        fee_amount=None,
        fee_currency_code=None,
        status="pending",
        amount_in_base=None,
        base_currency_code=None,
        fx_rate=None,
        provider_fingerprint=None,
        security_id=None,
    )
    session = SimpleNamespace(add=MagicMock(), flush=AsyncMock())
    uow = SimpleNamespace(
        session=session,
        transactions=SimpleNamespace(
            get_by_external_id=AsyncMock(side_effect=[existing, None])
        ),
    )
    updated = AsyncMock()
    created = AsyncMock()
    monkeypatch.setattr(persistence, "outbox_entity_updated", updated)
    monkeypatch.setattr(persistence, "outbox_entity_created", created)

    result = await TransactionPersistence("tenant-1").persist_transaction(
        uow, transaction, "account-1", security_id="security-1"
    )
    new_result = await TransactionPersistence("tenant-1").persist_transaction(
        uow, transaction, "account-1"
    )

    assert result is existing
    assert existing.revision == 2
    assert existing.security_id == "security-1"
    assert new_result.transaction_type.value == "other"
    assert new_result.status.value == "pending"
    updated.assert_awaited_once()
    created.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_active_query_scopes_by_tenant() -> None:
    webhook = SimpleNamespace(id="wh-1")

    class Session:
        async def execute(self, _stmt: object) -> SimpleNamespace:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [webhook])
            )

    service = WebhookService(
        lambda: _AsyncContext(Session()),
        SimpleNamespace(webhook_request_timeout_s=2.0),
    )

    result = await service._get_active_webhooks_for_event(
        "sync.completed", tenant_id="tenant-1"
    )

    assert result == [webhook]


@pytest.mark.asyncio
async def test_webhook_emit_event_closes_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.services.webhook import WebhookService

    dispatch = AsyncMock(return_value=2)
    close = AsyncMock()
    monkeypatch.setattr(WebhookService, "dispatch_event", dispatch)
    monkeypatch.setattr(WebhookService, "close", close)

    result = await WebhookService.emit_event(
        MagicMock(),
        SimpleNamespace(webhook_request_timeout_s=2.0),
        "sync.completed",
        {"value": 1},
    )

    assert result == 2
    dispatch.assert_awaited_once()
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_bunq_cards_job_reports_paused_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    settings = SimpleNamespace(
        worker_retry_max_attempts=1,
        worker_retry_base_delay_s=0.0,
    )
    session = SimpleNamespace(info={})
    container = SimpleNamespace(
        settings=settings,
        session_factory=lambda: _AsyncContext(session),
    )
    paused = {
        "tenant": SimpleNamespace(id="tenant-1"),
        "credential": SimpleNamespace(
            id="credential-1",
            status="paused",
            selected_accounts=[],
        ),
        "config": SimpleNamespace(options={}),
        "secrets": [],
    }

    async def connections(
        _uow: object, _provider: str
    ) -> list[dict[str, object]]:
        return [paused]

    monkeypatch.setattr(jobs, "_get_tenant_connections", connections)
    result = await jobs.sync_bunq_cards_job(container)

    assert result["skipped"] == 1
    assert result["results"][0]["reason"] == "paused"


@pytest.mark.asyncio
async def test_nightly_reconciliation_handles_empty_tenant_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    settings = SimpleNamespace()
    session = SimpleNamespace(info={}, commit=AsyncMock())
    container = SimpleNamespace(
        settings=settings,
        session_factory=lambda: _AsyncContext(session),
    )
    monkeypatch.setattr(
        jobs,
        "sync_bunq_job",
        AsyncMock(return_value={"provider": "bunq", "connections_synced": 0}),
    )
    monkeypatch.setattr(
        jobs,
        "sync_trading212_job",
        AsyncMock(
            return_value={"provider": "trading212", "connections_synced": 0}
        ),
    )
    monkeypatch.setattr(
        jobs,
        "UnitOfWork",
        lambda _session: SimpleNamespace(
            tenants=SimpleNamespace(list=AsyncMock(return_value=[]))
        ),
    )
    price_store = MagicMock()
    price_store.prune_intraday_data = AsyncMock(return_value=2)
    price_store.prune_hourly_data = AsyncMock(return_value=3)
    monkeypatch.setattr(
        "finance_sync.enrichment.price_store.PriceStore",
        MagicMock(return_value=price_store),
    )

    result = await jobs.nightly_reconciliation_job(container)

    assert result["status"] == "completed"
    assert {"pruned_minute_prices": 2, "pruned_hourly_prices": 3} in result[
        "results"
    ]
    session.commit.assert_awaited_once()


def test_degiro_import_helpers_cover_invalid_names_and_options(
    tmp_path: Path,
) -> None:
    from finance_sync.models.credential import Credential
    from finance_sync.services.degiro_import import (
        ImportValidationError,
        _formula_like,
        _safe_name,
        _tenant_stage,
        batch_hash,
        connector_options,
    )

    assert connector_options(Credential(description="not-json")) == {}
    assert connector_options(Credential(description="[]")) == {}
    assert _formula_like(" -12.50") is False
    assert _formula_like(" -SUM(A1)") is True
    assert batch_hash(["b", "a"]) == batch_hash(["a", "b"])
    settings = SimpleNamespace(degiro_import_staging_directory=tmp_path)
    staged = _tenant_stage(settings, "tenant-1", "run-1")
    assert staged.is_dir()
    assert staged.stat().st_mode & 0o777 == 0o700

    assert _safe_name("export.txt", 1)[1] == ".txt"
    assert _safe_name("expenses.json", 1)[1] == ".json"
    with pytest.raises(ImportValidationError, match="ondersteund"):
        _safe_name("export.pdf", 1)
    with pytest.raises(ImportValidationError, match="ongeldig pad"):
        _safe_name("../export.csv", 1)


def test_degiro_xlsx_archive_and_local_file_validation(tmp_path: Path) -> None:
    import zipfile

    from finance_sync.services.degiro_import import (
        ImportValidationError,
        _check_xlsx_archive,
        cleanup_expired_previews,
        validate_local_files,
    )

    unsafe = tmp_path / "unsafe.xlsx"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.xml", "x")
    with pytest.raises(ImportValidationError, match="onveilige"):
        _check_xlsx_archive(unsafe, 1024)

    invalid_xls = tmp_path / "invalid.xls"
    invalid_xls.write_bytes(b"not-an-xls")
    settings = SimpleNamespace(
        degiro_import_max_files=2,
        degiro_import_max_file_bytes=1024,
        degiro_import_max_batch_bytes=2048,
    )
    with pytest.raises(ImportValidationError, match="ongeldig XLS"):
        validate_local_files([invalid_xls], settings)
    assert (
        cleanup_expired_previews(
            SimpleNamespace(
                degiro_import_staging_directory=tmp_path / "missing"
            )
        )
        == 0
    )


def test_degiro_stage_paths_and_verification_errors(tmp_path: Path) -> None:
    from finance_sync.models.import_run import ImportRun
    from finance_sync.services.degiro_import import (
        ImportValidationError,
        stage_paths,
        verify_staged,
    )

    settings = SimpleNamespace(degiro_import_staging_directory=tmp_path)
    run = ImportRun(id="run-1", storage_names=["one.csv"], content_hashes=[])
    assert stage_paths(settings, "tenant-1", run)[0].name == "one.csv"
    with pytest.raises(ImportValidationError, match="niet meer compleet"):
        verify_staged(run, [tmp_path / "one.csv"])

    run.content_hashes = ["hash"]
    missing = tmp_path / "missing.csv"
    with pytest.raises(ImportValidationError, match="verlopen"):
        verify_staged(run, [missing])


@pytest.mark.asyncio
async def test_degiro_execute_run_completes_and_cleans_staged_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.models.enums import SyncRunStatus
    from finance_sync.models.import_run import ImportRun
    from finance_sync.services import degiro_import
    from finance_sync.sync.results import SyncResult

    staged_dir = tmp_path / "tenant" / "run"
    staged_dir.mkdir(parents=True)
    path = staged_dir / "transactions.csv"
    path.write_bytes(b"export")
    run = ImportRun(
        id="run-1",
        tenant_id="tenant-1",
        connection_id="connection-1",
        source="upload",
        status="previewed",
        batch_hash="batch-1",
        content_hashes=[hashlib.sha256(b"export").hexdigest()],
        storage_names=[path.name],
        rows_total=4,
        preview={"possible_duplicates": 1, "skipped": 2},
        audit_events=[],
    )
    result = SyncResult(
        status=SyncRunStatus.COMPLETED,
        accounts_synced=1,
        transactions_synced=3,
        holdings_synced=2,
        error_message=None,
        duration_s=0.1,
    )
    orchestrator = SimpleNamespace(run_sync=AsyncMock(return_value=result))
    monkeypatch.setattr(
        degiro_import, "SyncOrchestrator", lambda **_kwargs: orchestrator
    )
    monkeypatch.setattr(degiro_import, "ConnectorRegistry", MagicMock)
    session = SimpleNamespace(
        flush=AsyncMock(),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(id="account-1")
            )
        ),
    )
    container = SimpleNamespace(
        session_factory=MagicMock(),
        settings=SimpleNamespace(degiro_import_staging_directory=tmp_path),
    )

    completed = await degiro_import.execute_run(
        run,
        paths=[path],
        options={"account_key": "pension"},
        container=container,
        session=session,
    )

    assert completed.status == "completed"
    assert completed.created_count == 5
    assert completed.updated_count == 1
    assert completed.skipped_count == 2
    assert completed.account_id == "account-1"
    assert not path.exists()


@pytest.mark.asyncio
async def test_degiro_execute_run_marks_failed_result_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.models.enums import SyncRunStatus
    from finance_sync.models.import_run import ImportRun
    from finance_sync.services import degiro_import
    from finance_sync.sync.results import SyncResult

    staged_dir = tmp_path / "tenant" / "run"
    staged_dir.mkdir(parents=True)
    path = staged_dir / "transactions.csv"
    path.write_bytes(b"export")
    run = ImportRun(
        id="run-2",
        tenant_id="tenant-1",
        connection_id="connection-1",
        source="upload",
        status="previewed",
        batch_hash="batch-2",
        content_hashes=[hashlib.sha256(b"export").hexdigest()],
        storage_names=[path.name],
        rows_total=4,
        preview={},
        audit_events=[],
    )
    result = SyncResult(
        status=SyncRunStatus.FAILED,
        accounts_synced=0,
        transactions_synced=0,
        error_message="provider failed",
        duration_s=0.1,
    )
    orchestrator = SimpleNamespace(run_sync=AsyncMock(return_value=result))
    monkeypatch.setattr(
        degiro_import, "SyncOrchestrator", lambda **_kwargs: orchestrator
    )
    monkeypatch.setattr(degiro_import, "ConnectorRegistry", MagicMock)
    session = SimpleNamespace(flush=AsyncMock())
    container = SimpleNamespace(
        session_factory=MagicMock(),
        settings=SimpleNamespace(degiro_import_staging_directory=tmp_path),
    )

    with pytest.raises(degiro_import.ImportValidationError, match="atomair"):
        await degiro_import.execute_run(
            run,
            paths=[path],
            options={},
            container=container,
            session=session,
        )

    assert run.status == "failed"
    assert run.rejected_count == 4
    assert run.safe_error == "De import kon niet atomair worden verwerkt."
    assert not path.exists()


def test_worker_scheduler_helpers_cover_url_and_market_hours() -> None:
    from finance_sync.worker.scheduler import (
        _market_hours_cron,
        sync_jobstore_url,
    )

    assert sync_jobstore_url(None) is None
    assert sync_jobstore_url("") is None
    assert sync_jobstore_url("sqlite:///jobs.db") == "sqlite:///jobs.db"
    assert (
        sync_jobstore_url("postgresql+asyncpg://user:pw/db")
        == "postgresql+psycopg://user:pw/db"
    )
    trigger = _market_hours_cron(
        SimpleNamespace(
            worker_job_price_enrichment_market_open="09:30",
            worker_job_price_enrichment_market_close="16:00",
        ),
        minute_interval=5,
    )
    assert "minute='*/5'" in str(trigger)
    assert "hour='14-21'" in str(trigger)


@pytest.mark.asyncio
async def test_worker_scheduler_entrypoint_handles_missing_and_active_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_sync.worker.scheduler as scheduler_module

    await scheduler_module._monitored_job_entrypoint(
        987654, "missing", AsyncMock()
    )
    active = SimpleNamespace(run_monitored_job=AsyncMock())
    scheduler_module._schedulers[123] = active
    try:
        func = AsyncMock()
        await scheduler_module._monitored_job_entrypoint(123, "job-1", func)
        active.run_monitored_job.assert_awaited_once_with("job-1", func)
    finally:
        scheduler_module._schedulers.pop(123, None)


@pytest.mark.asyncio
async def test_worker_scheduler_monitored_job_clears_running_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_sync.worker.scheduler as scheduler_module

    class Context:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        scheduler_module, "JobRunContext", lambda *_args, **_kwargs: Context()
    )
    scheduler = scheduler_module.WorkerScheduler(
        SimpleNamespace(database_url=None), MagicMock(), MagicMock()
    )
    successful = AsyncMock()
    await scheduler.run_monitored_job("job-ok", successful)
    successful.assert_awaited_once()
    assert scheduler.running_jobs() == []

    failed = AsyncMock(side_effect=RuntimeError("boom"))
    await scheduler.run_monitored_job("job-failed", failed)
    assert scheduler.running_jobs() == []


@pytest.mark.asyncio
async def test_worker_scheduler_waits_for_running_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_sync.worker.scheduler as scheduler_module

    scheduler = scheduler_module.WorkerScheduler(
        SimpleNamespace(database_url=None), MagicMock(), MagicMock()
    )
    scheduler._running_jobs.add("job-1")

    async def clear_jobs(_delay: float) -> None:
        scheduler._running_jobs.clear()

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", clear_jobs)
    await scheduler.wait_for_completion()
    assert scheduler.running_jobs() == []


@pytest.mark.asyncio
async def test_worker_health_server_serves_and_stops() -> None:
    server = WorkerHealthServer(port=0)
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.05)
    assert server._server is not None
    await server.stop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_worker_health_ready_and_jobs_handlers_cover_states() -> None:
    scheduler = SimpleNamespace(is_running=lambda: True, job_summary=list)
    monitor = SimpleNamespace(summarize=lambda: [{"job": "sync"}])
    server = WorkerHealthServer(scheduler=scheduler, monitor=monitor)

    ready = await server._handle_ready(None)
    jobs = await server._handle_jobs(None)
    assert json.loads(ready.body._value.decode())["status"] == "ok"  # type: ignore[attr-defined]
    assert json.loads(jobs.body._value.decode())["jobs"] == [{"job": "sync"}]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_nightly_reconciliation_isolates_tenant_and_pruning_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.worker import jobs

    session = SimpleNamespace(info={}, commit=AsyncMock())
    tenants = [
        SimpleNamespace(id="tenant-ok"),
        SimpleNamespace(id="tenant-bad"),
    ]
    container = SimpleNamespace(
        settings=SimpleNamespace(),
        session_factory=lambda: _AsyncContext(session),
    )
    monkeypatch.setattr(
        jobs,
        "sync_bunq_job",
        AsyncMock(side_effect=RuntimeError("bunq down")),
    )
    monkeypatch.setattr(
        jobs,
        "sync_trading212_job",
        AsyncMock(side_effect=RuntimeError("trading down")),
    )
    monkeypatch.setattr(
        jobs,
        "UnitOfWork",
        lambda _session: SimpleNamespace(
            tenants=SimpleNamespace(list=AsyncMock(return_value=tenants))
        ),
    )

    class Orchestrator:
        def __init__(self, **kwargs: object) -> None:
            self.tenant_id = kwargs["tenant_id"]

        async def run_reconciliation(self) -> object:
            if self.tenant_id == "tenant-bad":
                message = "tenant failure"
                raise RuntimeError(message)
            return SimpleNamespace(
                run_id="reconciliation-1",
                status=SimpleNamespace(value="completed"),
                finding_count=2,
            )

    monkeypatch.setattr(jobs, "SyncOrchestrator", Orchestrator)
    prune_store = MagicMock()
    prune_store.prune_intraday_data = AsyncMock(side_effect=RuntimeError("db"))
    monkeypatch.setattr(
        "finance_sync.enrichment.price_store.PriceStore",
        MagicMock(return_value=prune_store),
    )

    result = await jobs.nightly_reconciliation_job(container)

    assert result["status"] == "completed"
    assert {
        item["tenant_id"] for item in result["results"] if "tenant_id" in item
    } == {
        "tenant-ok",
        "tenant-bad",
    }
    failed = next(
        item
        for item in result["results"]
        if item.get("tenant_id") == "tenant-bad"
    )
    assert failed["error"] == "tenant failure"
    assert session.commit.await_count == 0


@pytest.mark.asyncio
async def test_worker_scheduler_skips_stop_and_pause_when_not_running() -> None:
    from finance_sync.worker.scheduler import WorkerScheduler

    scheduler = WorkerScheduler(
        SimpleNamespace(database_url=None), MagicMock(), MagicMock()
    )
    scheduler._scheduler = SimpleNamespace(
        running=False,
        shutdown=MagicMock(),
        pause=MagicMock(),
        resume=MagicMock(),
    )

    scheduler.pause()
    await scheduler.stop()
    assert scheduler._scheduler.shutdown.call_count == 0


@pytest.mark.asyncio
async def test_worker_scheduler_registers_trading_price_and_reconciliation_jobs() -> (
    None
):
    from finance_sync.worker.scheduler import WorkerScheduler

    settings = SimpleNamespace(
        database_url=None,
        worker_job_schedules_enabled=False,
        worker_job_intel_enabled=False,
        worker_job_holding_relevance_enabled=False,
        worker_job_bunq_sync_enabled=False,
        worker_job_bunq_cards_enabled=False,
        worker_job_trading212_sync_enabled=True,
        worker_job_trading212_sync_interval_hours=6,
        worker_job_degiro_watch_enabled=False,
        worker_job_price_enrichment_enabled=True,
        worker_job_price_enrichment_market_open="09:30",
        worker_job_price_enrichment_market_close="16:00",
        worker_job_price_enrichment_interval_minutes=15,
        worker_job_reconciliation_enabled=True,
        worker_job_reconciliation_cron="0 2 * * *",
        worker_job_outbox_enabled=False,
        worker_job_export_enabled=False,
    )
    scheduler = WorkerScheduler(settings, MagicMock(), MagicMock())
    scheduler._register_jobs()
    try:
        ids = set(scheduler._job_ids)
    finally:
        await scheduler.stop()

    assert ids == {"sync_trading212", "enrich_prices", "nightly_reconciliation"}

    invalid = SimpleNamespace(
        **{**settings.__dict__, "worker_job_reconciliation_cron": "invalid"}
    )
    invalid_scheduler = WorkerScheduler(invalid, MagicMock(), MagicMock())
    invalid_scheduler._register_jobs()
    try:
        invalid_ids = set(invalid_scheduler._job_ids)
    finally:
        await invalid_scheduler.stop()
    assert "nightly_reconciliation" not in invalid_ids


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/hook",
        "http://localhost:8080/hook",
        "http://127.0.0.1/hook",
        "http://service.localhost/hook",
    ],
)
def test_webhook_url_validation_accepts_supported_destinations(
    url: str,
) -> None:
    validate_webhook_url(url)


def test_webhook_url_validation_rejects_private_https_and_non_http() -> None:
    with pytest.raises(ValueError, match="Private"):
        validate_webhook_url("https://127.0.0.1/hook")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_webhook_url("ftp://example.test/hook")


@pytest.mark.asyncio
async def test_webhook_client_lifecycle_and_redis_rate_limit() -> None:
    settings = SimpleNamespace(webhook_request_timeout_s=2.0)
    redis = SimpleNamespace(
        incr=AsyncMock(return_value=1),
        expire=AsyncMock(),
    )
    service = WebhookService(MagicMock(), settings, redis_client=redis)
    client = service.http_client
    assert client is service.http_client
    await service.close()
    assert service._http_client is None

    webhook = SimpleNamespace(id="wh-1", rate_limit_max_per_minute=5)
    assert await service._is_rate_allowed(webhook) is True
    redis.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_redis_rate_limit_falls_back_on_redis_error() -> None:
    from finance_sync.services.webhook import WebhookService

    redis = SimpleNamespace(incr=AsyncMock(side_effect=RuntimeError("down")))
    service = WebhookService(
        MagicMock(), SimpleNamespace(webhook_request_timeout_s=2.0), redis
    )
    webhook = SimpleNamespace(id="wh-fallback", rate_limit_max_per_minute=5)

    assert await service._is_rate_allowed(webhook) is True


@pytest.mark.asyncio
async def test_webhook_retry_cancels_inactive_delivery() -> None:
    from finance_sync.models.enums import WebhookDeliveryStatus

    pending_log = SimpleNamespace(
        id="log-1",
        webhook_id="wh-1",
        status=WebhookDeliveryStatus.FAILED.value,
        next_retry_at=datetime.now(UTC),
        attempt_number=1,
        max_attempts=3,
    )
    row = SimpleNamespace(id="log-1")
    calls = 0

    class Session:
        async def execute(self, _stmt: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: [pending_log])
                )
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def get(self, _model: object, _id: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    service = WebhookService(
        lambda: _AsyncContext(Session()),
        SimpleNamespace(webhook_request_timeout_s=2.0),
    )

    assert await service.retry_due_deliveries() == 0
    assert calls >= 2


@pytest.mark.asyncio
async def test_webhook_retry_reloads_active_delivery_and_commits() -> None:
    from finance_sync.models.enums import WebhookDeliveryStatus

    pending_log = SimpleNamespace(
        id="log-2",
        webhook_id="wh-2",
        status=WebhookDeliveryStatus.FAILED.value,
        next_retry_at=datetime.now(UTC),
        attempt_number=1,
        max_attempts=3,
    )
    fresh = SimpleNamespace(id="log-2", attempt_number=1)
    webhook = SimpleNamespace(id="wh-2", is_active=True)
    calls = 0
    commits = 0

    class Session:
        async def execute(self, _stmt: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: [pending_log])
                )
            return SimpleNamespace(scalar_one_or_none=lambda: fresh)

        async def get(self, _model: object, _id: object) -> object:
            return webhook

        async def commit(self) -> None:
            nonlocal commits
            commits += 1

    service = WebhookService(
        lambda: _AsyncContext(Session()),
        SimpleNamespace(webhook_request_timeout_s=2.0),
    )
    service._attempt_delivery = AsyncMock()  # type: ignore[method-assign]

    assert await service.retry_due_deliveries() == 1
    assert fresh.attempt_number == 2
    assert commits == 1
    service._attempt_delivery.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "request", "unexpected"])
async def test_webhook_delivery_failure_modes_schedule_or_record_retry(
    failure: str,
) -> None:
    import httpx

    from finance_sync.models.enums import WebhookDeliveryStatus

    settings = SimpleNamespace(
        webhook_request_timeout_s=2.0,
        webhook_retry_base_delay_s=1.0,
    )
    service = WebhookService(MagicMock(), settings)
    service._is_rate_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    webhook = SimpleNamespace(
        id="wh-failure",
        tenant_id="tenant-1",
        url="https://example.test/hook",
    )
    log_entry = SimpleNamespace(
        id="log-failure",
        payload={"event": "sync"},
        status=WebhookDeliveryStatus.PENDING,
        attempt_number=1,
        max_attempts=3,
        next_retry_at=None,
        response_status_code=None,
        response_body=None,
        duration_ms=None,
        error_message=None,
    )
    request = httpx.Request("POST", webhook.url)
    if failure == "http":
        response = SimpleNamespace(status_code=503, text="service unavailable")
        service._http_client = SimpleNamespace(
            post=AsyncMock(return_value=response)
        )  # type: ignore[assignment]
    elif failure == "request":
        service._http_client = SimpleNamespace(  # type: ignore[assignment]
            post=AsyncMock(
                side_effect=httpx.RequestError("network", request=request)
            )
        )
    else:
        service._http_client = SimpleNamespace(  # type: ignore[assignment]
            post=AsyncMock(side_effect=ValueError("bad response"))
        )
    session = SimpleNamespace(execute=AsyncMock())

    await service._attempt_delivery(log_entry, webhook, session)

    assert log_entry.status == WebhookDeliveryStatus.FAILED
    assert log_entry.response_status_code in {0, 503}
    assert log_entry.next_retry_at is not None
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_dispatch_delivers_all_targets_and_commits() -> None:
    settings = SimpleNamespace(webhook_request_timeout_s=2.0)
    session = SimpleNamespace(commit=AsyncMock())
    service = WebhookService(lambda: _AsyncContext(session), settings)
    webhook = SimpleNamespace(id="wh-1")
    service._get_active_webhooks_for_event = AsyncMock(return_value=[webhook])  # type: ignore[method-assign]
    service._deliver = AsyncMock()  # type: ignore[method-assign]

    count = await service.dispatch_event(
        "sync.completed", {"tenant_id": "tenant-1"}, event_id="event-1"
    )

    assert count == 1
    service._deliver.assert_awaited_once_with(  # type: ignore[attr-defined]
        webhook,
        "sync.completed",
        {"tenant_id": "tenant-1"},
        "event-1",
        session,
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_crud_is_tenant_scoped_with_fake_unit_of_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_sync.db.uow as uow_module

    class Repository:
        def __init__(self) -> None:
            self.items: list[object] = []

        async def add(self, item: object) -> object:
            if item.id is None:
                item.id = "webhook-1"
            self.items.append(item)
            return item

        async def list(self, *_filters: object) -> list[object]:
            return list(self.items)

        async def get(self, item_id: str) -> object | None:
            return next(
                (item for item in self.items if item.id == item_id), None
            )

        async def delete(self, item: object) -> None:
            self.items.remove(item)

    repository = Repository()
    commits = 0

    class UnitOfWork:
        def __init__(self, _session: object) -> None:
            self.webhooks = repository

        async def __aenter__(self) -> UnitOfWork:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            nonlocal commits
            commits += 1

    monkeypatch.setattr(uow_module, "UnitOfWork", UnitOfWork)
    service = WebhookService(
        lambda: object(), SimpleNamespace(webhook_request_timeout_s=2.0)
    )

    created = await service.create_webhook(
        "tenant-1",
        "https://example.test/hook",
        ["sync.completed"],
        description="test hook",
    )
    assert created.tenant_id == "tenant-1"
    assert created.secret
    assert await service.list_webhooks(
        "tenant-1", event_type="sync.completed"
    ) == [created]
    assert await service.get_webhook(str(created.id), "tenant-2") is None
    assert await service.get_webhook(str(created.id), "tenant-1") is created
    assert await service.delete_webhook(str(created.id), "tenant-2") is False
    assert await service.delete_webhook(str(created.id), "tenant-1") is True
    assert commits == 2


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.test/hook",
        "http://example.test/hook",
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
    ],
)
def test_webhook_url_validation_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_webhook_url_validation_resolve_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_lookup(*_args: object, **_kwargs: object) -> object:
        message = "dns unavailable"
        raise OSError(message)

    monkeypatch.setattr(
        "finance_sync.services.webhook.socket.getaddrinfo", fail_lookup
    )
    with pytest.raises(ValueError, match="could not be resolved"):
        validate_webhook_url("https://unresolvable.example", resolve=True)


def test_webhook_sliding_window_counter_prunes_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(
        "finance_sync.services.webhook.time.monotonic", lambda: now
    )
    counter = _SlidingWindowCounter()
    assert counter.is_allowed("hook", 2, window_s=60) is True
    assert counter.is_allowed("hook", 2, window_s=60) is True
    assert counter.is_allowed("hook", 2, window_s=60) is False
    now = 161.0
    assert counter.is_allowed("hook", 2, window_s=60) is True


def test_webhook_signatures_round_trip() -> None:
    payload = {"event": "sync.completed", "value": 2}
    signature = WebhookService._sign_payload(payload, "secret")
    assert len(signature) == 64
    assert WebhookService.verify_signature(payload, signature, "secret")
    assert not WebhookService.verify_signature(payload, signature, "wrong")
    assert len(WebhookService._generate_secret()) == 64


@pytest.mark.asyncio
async def test_webhook_rate_limit_uses_redis_and_falls_back() -> None:
    redis = MagicMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock()
    service = WebhookService(MagicMock(), MagicMock(), redis)
    webhook = SimpleNamespace(id="hook", rate_limit_max_per_minute=2)

    assert await service._is_rate_allowed(webhook) is True
    redis.incr.assert_awaited_once()
    redis.expire.assert_awaited_once()

    redis.incr = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await service._is_rate_allowed(webhook) is True


@pytest.mark.asyncio
async def test_webhook_http_client_is_reused_and_closed() -> None:
    settings = SimpleNamespace(webhook_request_timeout_s=1.0)
    service = WebhookService(MagicMock(), settings)
    first = service.http_client
    assert service.http_client is first
    await service.close()
    assert service._http_client is None


@pytest.mark.asyncio
async def test_performance_twr_links_periods_and_cash_flow() -> None:
    service = PerformanceService(MagicMock())
    start = datetime(2025, 1, 1, tzinfo=UTC)
    middle = datetime(2025, 1, 2, tzinfo=UTC)
    end = datetime(2025, 1, 3, tzinfo=UTC)
    service._get_daily_portfolio_values = AsyncMock(  # type: ignore[method-assign]
        return_value=[(start, 100), (middle, 110), (end, 132)]
    )
    service._get_external_cash_flows = AsyncMock(  # type: ignore[method-assign]
        return_value=[(middle, -10)]
    )

    result = await service.calculate_twr(
        "tenant-1", date_from=start, date_to=end, annualized=False
    )

    assert result.total_return_pct == 20
    assert result.annualized_return_pct is None
    assert len(result.periods) == 2
    assert result.periods[0].external_cash_flow == 10


@pytest.mark.asyncio
async def test_performance_mwr_handles_cash_flows() -> None:
    service = PerformanceService(MagicMock())
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    service._get_portfolio_value_at = AsyncMock(  # type: ignore[method-assign]
        side_effect=[100, 121]
    )
    service._get_external_cash_flows = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )

    result = await service.calculate_mwr(
        "tenant-1", date_from=start, date_to=end
    )

    assert result.initial_value == 100
    assert result.final_value == 121
    assert result.converged is True
    assert float(result.internal_rate_of_return_pct) == pytest.approx(
        21, abs=0.1
    )


@pytest.mark.asyncio
async def test_performance_benchmark_calculates_statistics() -> None:
    service = PerformanceService(MagicMock())
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 4, tzinfo=UTC)
    service.calculate_twr = AsyncMock(  # type: ignore[method-assign]
        return_value=TWRResponse(total_return_pct=10)
    )
    service._resolve_benchmark = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id="index-1", name="World Index")
    )
    service._get_security_price_series = AsyncMock(  # type: ignore[method-assign]
        return_value=[Decimal(100), Decimal(105), Decimal(110)]
    )
    service._get_daily_returns = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            (datetime(2025, 1, 2, tzinfo=UTC), Decimal("0.04")),
            (datetime(2025, 1, 3, tzinfo=UTC), Decimal("0.05")),
        ]
    )

    result = await service.benchmark_comparison(
        "tenant-1", date_from=start, date_to=end
    )

    assert result.benchmark_name == "World Index"
    assert result.benchmark_return_pct == 10
    assert result.alpha_pct is not None
    assert result.beta is not None
    assert result.correlation is not None


@pytest.mark.asyncio
async def test_performance_attribution_builds_components() -> None:
    service = PerformanceService(MagicMock())
    service._get_security_type_weights = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"equity": 1}, {"equity": 1}]
    )
    service._resolve_benchmark = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    service._get_sector_returns = AsyncMock(  # type: ignore[method-assign]
        return_value={"equity": Decimal("0.1")}
    )

    result = await service.attribution("tenant-1")

    assert isinstance(result, AttributionResponse)
    assert len(result.components) == 1
    assert result.components[0].name == "equity"


@pytest.mark.asyncio
async def test_performance_summary_composes_services_and_coverage() -> None:
    service = PerformanceService(MagicMock())
    twr = TWRResponse(total_return_pct=1)
    mwr = MWRResponse(
        internal_rate_of_return_pct=1,
        initial_value=100,
        final_value=101,
        total_cash_flows=0,
        cash_flow_count=0,
        converged=True,
    )
    bench = BenchmarkComparisonResponse(
        portfolio_return_pct=1,
        benchmark_return_pct=1,
        benchmark_name="Index",
    )
    attr = AttributionResponse(
        total_allocation_effect_pct=0,
        total_selection_effect_pct=0,
        total_interaction_effect_pct=0,
        total_excess_return_pct=0,
    )
    service.calculate_twr = AsyncMock(return_value=twr)  # type: ignore[method-assign]
    service.calculate_mwr = AsyncMock(return_value=mwr)  # type: ignore[method-assign]
    service.benchmark_comparison = AsyncMock(return_value=bench)  # type: ignore[method-assign]
    service.attribution = AsyncMock(return_value=attr)  # type: ignore[method-assign]
    service._get_valuation_coverage = AsyncMock(  # type: ignore[method-assign]
        return_value=(datetime(2025, 1, 1, tzinfo=UTC), 2, 3)
    )

    result = await service.get_summary("tenant-1")

    assert result.twr is twr
    assert result.mwr is mwr
    assert result.benchmark is bench
    assert result.attribution is attr
    assert result.meta.coverage.accounts == 2


@pytest.mark.asyncio
async def test_security_resolution_honours_mapping_and_figi_fallback() -> None:
    from finance_sync.connectors.models import SecurityReference
    from finance_sync.sync.persistence import SecurityPersistence

    resolved = SimpleNamespace(id="security-resolved")
    uow = SimpleNamespace(
        unresolved_securities=SimpleNamespace(
            list=AsyncMock(
                return_value=[
                    SimpleNamespace(resolved_security_id="security-resolved")
                ]
            )
        ),
        securities=SimpleNamespace(get=AsyncMock(return_value=resolved)),
    )
    result, unresolved = await SecurityPersistence(
        "tenant-1"
    ).resolve_security_reference(
        uow,
        "broker",
        SecurityReference(external_id="provider-id", isin="US0378331005"),
    )
    assert result is resolved
    assert unresolved is None

    candidate = SimpleNamespace(id="security-figi")
    uow = SimpleNamespace(
        unresolved_securities=SimpleNamespace(list=AsyncMock(return_value=[])),
        securities=SimpleNamespace(list=AsyncMock(return_value=[candidate])),
    )
    result, unresolved = await SecurityPersistence(
        "tenant-1"
    ).resolve_security_reference(
        uow, "broker", SecurityReference(figi="BBG000B9XRY4")
    )
    assert result is candidate
    assert unresolved is None


@pytest.mark.asyncio
async def test_security_resolution_filters_ticker_and_queues_ambiguity() -> (
    None
):
    from finance_sync.connectors.models import SecurityReference
    from finance_sync.sync.persistence import SecurityPersistence

    eur = SimpleNamespace(id="security-eur", currency_code="EUR")
    usd = SimpleNamespace(id="security-usd", currency_code="USD")
    uow = SimpleNamespace(
        unresolved_securities=SimpleNamespace(
            list=AsyncMock(side_effect=[[], []])
        ),
        securities=SimpleNamespace(list=AsyncMock(return_value=[eur, usd])),
        session=SimpleNamespace(add=MagicMock(), flush=AsyncMock()),
    )
    result, unresolved = await SecurityPersistence(
        "tenant-1"
    ).resolve_security_reference(
        uow,
        "broker",
        SecurityReference(
            ticker="VWCE", currency_code="EUR", external_id="provider-vwce"
        ),
    )
    assert result is None
    assert unresolved == "provider-vwce"

    uow = SimpleNamespace(
        unresolved_securities=SimpleNamespace(list=AsyncMock(return_value=[])),
        securities=SimpleNamespace(list=AsyncMock(return_value=[eur, usd])),
    )
    result, unresolved = await SecurityPersistence(
        "tenant-1"
    ).resolve_security_reference(
        uow, "broker", SecurityReference(ticker="VWCE", currency_code="EUR")
    )
    assert result is eur
    assert unresolved is None


@pytest.mark.asyncio
async def test_security_resolution_creates_instrument_and_updates_queue() -> (
    None
):
    from finance_sync.connectors.models import SecurityReference
    from finance_sync.models.enums import SecurityType
    from finance_sync.sync.persistence import SecurityPersistence

    session = SimpleNamespace(add=MagicMock(), flush=AsyncMock())
    uow = SimpleNamespace(
        unresolved_securities=SimpleNamespace(list=AsyncMock(return_value=[])),
        securities=SimpleNamespace(list=AsyncMock(return_value=[])),
        session=session,
    )
    result, unresolved = await SecurityPersistence(
        "tenant-1"
    ).resolve_security_reference(
        uow,
        "broker",
        SecurityReference(
            external_id="provider-id",
            ticker="ABC",
            name="Alpha",
            currency_code="eur",
            security_type="not-a-real-type",
            provider_metadata={"source": "test"},
            venue="XETRA",
        ),
    )
    assert result is not None
    assert result.security_type == SecurityType.OTHER
    assert result.currency_code == "EUR"
    assert unresolved is None
    assert session.add.call_count == 2
    assert session.flush.await_count == 2
    assert (
        session.add.call_args.args[0].raw_metadata
        == '{"source": "test", "venue": "XETRA"}'
    )

    existing = SimpleNamespace()
    uow = SimpleNamespace(
        unresolved_securities=SimpleNamespace(
            list=AsyncMock(return_value=[existing])
        ),
        session=SimpleNamespace(flush=AsyncMock()),
    )
    result = await SecurityPersistence("tenant-1")._queue_unresolved_security(
        uow,
        "broker",
        SecurityReference(external_id="provider-id", ticker="ABC"),
        resolved_security_id="security-1",
        resolution_method="manual",
    )
    assert result == "provider-id"
    assert existing.resolved_security_id == "security-1"
    assert existing.resolution_method == "manual"


@pytest.mark.asyncio
async def test_sync_persistence_routes_context_and_rejects_card_writes_without_it() -> (
    None
):
    from finance_sync.sync.persistence import (
        PersistenceContext,
        SyncPersistence,
    )

    writer = SimpleNamespace(
        persist_holding=AsyncMock(return_value="writer-holding"),
        resolve_security_reference=AsyncMock(
            return_value=("writer-security", None)
        ),
    )
    persistence = SyncPersistence(writer)
    assert (
        await persistence.persist_holding(
            "uow", "holding", "account", "security"
        )
        == "writer-holding"
    )
    assert await persistence.resolve_security_reference(
        "uow", "broker", "reference"
    ) == ("writer-security", None)
    with pytest.raises(ValueError, match="scheduled-payment"):
        await persistence.persist_scheduled_payment(
            "uow", "schedule", "account"
        )
    with pytest.raises(ValueError, match="card-transaction"):
        await persistence.persist_card_transaction("uow", "card")

    persistence = SyncPersistence(
        SimpleNamespace(), context=PersistenceContext("tenant-1", "broker")
    )
    persistence._holding_persistence.persist_holding = AsyncMock(
        return_value="context-holding"
    )
    persistence._security_persistence.resolve_security_reference = AsyncMock(
        return_value=("context-security", None)
    )
    assert (
        await persistence.persist_holding(
            "uow", "holding", "account", "security"
        )
        == "context-holding"
    )
    assert await persistence.resolve_security_reference(
        "uow", "broker", "reference"
    ) == ("context-security", None)


def test_exporter_api_lists_enabled_types_and_builds_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.api.v1 import exporters

    settings = SimpleNamespace(
        exporter_wealthfolio_enabled=True,
        exporter_actual_budget_enabled=True,
        exporter_firefly_enabled=True,
        wealthfolio_output_dir="/tmp/exports",
        wealthfolio_default_currency="USD",
        wealthfolio_export_holdings=False,
        wealthfolio_max_transactions_per_file=42,
        wealthfolio_include_pending=True,
        wealthfolio_account_name_overrides={"a": "Main"},
        wealthfolio_instrument_type_overrides={"ETF": "fund"},
        wealthfolio_holdings_strategy="bootstrap",
        wealthfolio_reconciliation_absolute_tolerance=Decimal("2.00"),
        wealthfolio_reconciliation_percentage_tolerance=Decimal("0.01"),
    )
    container = SimpleNamespace(settings=settings)
    monkeypatch.setattr(exporters, "get_container", lambda _request: container)
    request = SimpleNamespace()

    types = asyncio.run(exporters.list_exporter_types(request))
    assert [item.name for item in types] == [
        "wealthfolio",
        "actual-budget",
        "firefly",
    ]
    config = exporters._build_wealthfolio_config(container)
    assert config.output_dir == Path("/tmp/exports")
    assert config.default_currency == "USD"
    assert config.max_transactions_per_file == 42
    assert config.account_name_overrides == {"a": "Main"}


def test_exporter_api_flags_and_run_id_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import exporters

    disabled = SimpleNamespace(
        exporter_wealthfolio_enabled=False,
        exporter_firefly_enabled=False,
    )
    monkeypatch.setattr(
        exporters,
        "get_container",
        lambda _request: SimpleNamespace(settings=disabled),
    )
    with pytest.raises(HTTPException) as error:
        exporters._require_wealthfolio_enabled(SimpleNamespace())
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        exporters._require_firefly_enabled(SimpleNamespace())
    assert error.value.status_code == 404
    assert exporters._parse_run_id("not-a-uuid") is None
    assert (
        exporters._parse_run_id("00000000-0000-0000-0000-000000000001")
        is not None
    )


@pytest.mark.asyncio
async def test_exporter_api_lists_runs_and_gets_run_details() -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import exporters

    started = datetime(2025, 1, 1, tzinfo=UTC)
    completed = datetime(2025, 1, 1, 0, 0, 2, tzinfo=UTC)
    run = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        tenant_id="tenant-1",
        exporter_type="wealthfolio",
        status="failed",
        started_at=started,
        completed_at=completed,
        transactions_attempted=4,
        transactions_exported=3,
        transactions_failed=1,
        error_message="connection timeout",
        error_category=None,
        target_id="target-1",
        account_scope=["account-1"],
        delivery_checkpoint={"cursor": "4"},
    )
    count_result = SimpleNamespace(all=lambda: [run])
    page_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [run])
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[count_result, page_result])
    )
    auth = SimpleNamespace(tenant_id="tenant-1")

    response = await exporters.list_export_runs(
        auth=auth, db=db, status_filter="error", limit=25, offset=0
    )
    assert response.total == 1
    assert response.runs[0].duration_seconds == 2
    assert response.runs[0].error_category == "provider_unavailable"

    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: run)
        )
    )
    detail = await exporters.get_export_run(run.id, auth=auth, db=db)
    assert detail.id == run.id
    assert detail.delivery_checkpoint == {"cursor": "4"}

    with pytest.raises(HTTPException) as error:
        await exporters.get_export_run("invalid", auth=auth, db=db)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_lifespan_database_initialisation_updates_existing_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync import lifespan as lifespan_module

    class _Row:
        def __init__(self, value: object) -> None:
            self.value = value

        def first(self) -> object:
            return self.value

    conn = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(),
                _Row(("tenant-1",)),
                _Row(("user-1", "old-hash")),
                SimpleNamespace(),
            ]
        ),
        commit=AsyncMock(),
    )
    settings = SimpleNamespace(
        admin_key="ignored",
        is_production=True,
        is_staging=False,
    )
    container = SimpleNamespace(
        settings=settings,
        engine=SimpleNamespace(
            begin=MagicMock(return_value=_AsyncContext(conn))
        ),
    )
    monkeypatch.setattr(
        lifespan_module, "secret_value", lambda _value: "a" * 32
    )
    monkeypatch.setattr(lifespan_module, "_DB_RETRIES", 1)
    monkeypatch.setattr(
        "finance_sync.services.auth.hash_password", lambda _value: "new-hash"
    )

    await lifespan_module._init_database(container)

    assert conn.execute.await_count == 4
    conn.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_database_initialisation_creates_tenant_and_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync import lifespan as lifespan_module

    class _Row:
        def __init__(self, value: object) -> None:
            self.value = value

        def first(self) -> object:
            return self.value

        def scalar_one(self) -> str:
            return "tenant-created"

    conn = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(),
                _Row(None),
                _Row(None),
                _Row(None),
                SimpleNamespace(),
            ]
        ),
        commit=AsyncMock(),
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(
            admin_key="ignored", is_production=True, is_staging=False
        ),
        engine=SimpleNamespace(
            begin=MagicMock(return_value=_AsyncContext(conn))
        ),
    )
    monkeypatch.setattr(
        lifespan_module, "secret_value", lambda _value: "b" * 32
    )
    monkeypatch.setattr(lifespan_module, "_DB_RETRIES", 1)
    monkeypatch.setattr(
        "finance_sync.services.auth.hash_password", lambda _value: "hash"
    )

    await lifespan_module._init_database(container)

    assert conn.execute.await_count == 5
    conn.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_database_initialisation_retries_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync import lifespan as lifespan_module

    engine = SimpleNamespace(
        begin=MagicMock(side_effect=RuntimeError("database unavailable"))
    )
    container = SimpleNamespace(engine=engine, settings=SimpleNamespace())
    sleeps = AsyncMock()
    monkeypatch.setattr(
        lifespan_module, "asyncio", SimpleNamespace(sleep=sleeps)
    )
    monkeypatch.setattr(lifespan_module, "_DB_RETRIES", 2)
    monkeypatch.setattr(lifespan_module, "_DB_RETRY_DELAY_S", 0.01)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await lifespan_module._init_database(container)
    sleeps.assert_awaited_once_with(0.01)


@pytest.mark.asyncio
async def test_lifespan_bootstraps_legacy_export_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync import lifespan as lifespan_module

    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[SimpleNamespace(id="tenant-1"), None, None]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    settings = SimpleNamespace(
        wealthfolio_password="ignored",
        wealthfolio_server_url="http://wealthfolio",
        actual_budget_password="ignored",
        actual_budget_server_url="http://actual",
        actual_budget_budget_name="Budget",
        actual_budget_sync_id="sync-1",
        actual_budget_encryption_password="encrypt",
    )
    container = SimpleNamespace(
        settings=settings,
        session_factory=MagicMock(return_value=_AsyncContext(session)),
    )
    monkeypatch.setattr(
        lifespan_module, "secret_value", lambda _value: "secret"
    )
    monkeypatch.setattr(
        "finance_sync.services.auth.encrypt_credential",
        lambda _payload, _settings: ("encrypted", "nonce"),
    )
    monkeypatch.setattr(
        "finance_sync.services.sync_schedule.SyncScheduleService.ensure_for_scope",
        AsyncMock(return_value=SimpleNamespace(id="schedule-1")),
    )

    await lifespan_module._bootstrap_legacy_export_targets(container)

    assert session.add.call_count == 2
    assert session.flush.await_count == 2
    session.commit.assert_awaited_once()
    assert {
        item.target_type
        for item in (call.args[0] for call in session.add.call_args_list)
    } == {
        "wealthfolio",
        "actual-budget",
    }


@pytest.mark.asyncio
async def test_exporter_retry_rejects_invalid_and_disabled_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import exporters

    auth = SimpleNamespace(tenant_id="tenant-1")
    base = SimpleNamespace(
        exporter_wealthfolio_enabled=False,
        exporter_actual_budget_enabled=False,
        exporter_firefly_enabled=False,
        redis_url=None,
    )
    container = SimpleNamespace(settings=base)
    db = SimpleNamespace()
    monkeypatch.setattr(exporters, "get_container", lambda _request: container)

    for exporter_type in ("wealthfolio", "actual-budget", "firefly", "unknown"):
        with pytest.raises(HTTPException) as error:
            await exporters._retry_export_run_locked(
                exporter_type,
                "00000000-0000-0000-0000-000000000001",
                SimpleNamespace(),
                auth,
                db,
                SimpleNamespace(),
                container,
            )
        assert error.value.status_code == 404

    with pytest.raises(HTTPException) as error:
        await exporters.retry_export_run(
            "wealthfolio",
            "not-a-uuid",
            SimpleNamespace(),
            _auth=auth,
            db=db,
        )
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_exporter_retry_runs_each_enabled_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync.api.v1 import exporters

    settings = SimpleNamespace(
        exporter_wealthfolio_enabled=True,
        exporter_actual_budget_enabled=True,
        exporter_firefly_enabled=True,
    )
    container = SimpleNamespace(
        settings=settings,
        session_factory=MagicMock(),
    )
    auth = SimpleNamespace(tenant_id="tenant-1")
    run = SimpleNamespace(status="failed", exporter_type=None)

    def database() -> SimpleNamespace:
        return SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    scalar_one_or_none=lambda run=run: run
                )
            )
        )

    outcome = SimpleNamespace(
        run_id="retry-run",
        status="completed",
        transactions_attempted=2,
        transactions_exported=2,
        transactions_failed=0,
        error_message=None,
        duration_s=0.25,
    )

    class _Exporter:
        def __init__(self, **_kwargs: object) -> None:
            self.run_export = AsyncMock(return_value=outcome)

    monkeypatch.setattr(exporters, "WealthfolioExporter", _Exporter)
    monkeypatch.setattr(exporters, "ActualBudgetExporter", _Exporter)
    monkeypatch.setattr(exporters, "FireflyExporter", _Exporter)
    monkeypatch.setattr(
        exporters.ActualBudgetConfig,
        "from_settings",
        MagicMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        exporters.FireflyConfig,
        "from_settings",
        MagicMock(return_value=SimpleNamespace()),
    )

    for exporter_type in ("wealthfolio", "actual-budget", "firefly"):
        response = await exporters._retry_export_run_locked(
            exporter_type,
            "00000000-0000-0000-0000-000000000001",
            SimpleNamespace(),
            auth,
            database(),
            run,
            container,
        )
        assert response.run_id == "retry-run"
        assert response.status == "completed"


def test_sync_error_classification_covers_operational_categories() -> None:
    from finance_sync.connectors.exceptions import (
        ConnectorError,
        PermanentError,
        RateLimitError,
        TransientError,
    )
    from finance_sync.sync.errors import (
        SyncErrorKind,
        categorize_export_error,
        categorize_sync_error,
        classify_sync_error,
        safe_sync_error_message,
    )

    assert (
        classify_sync_error(TransientError("temporary"))
        == SyncErrorKind.TRANSIENT
    )
    assert (
        classify_sync_error(PermanentError("invalid input"))
        == SyncErrorKind.PERMANENT
    )
    assert (
        classify_sync_error(RuntimeError("internal")) == SyncErrorKind.INTERNAL
    )
    assert safe_sync_error_message(ConnectorError()) == "Connector sync failed"
    assert (
        safe_sync_error_message(RuntimeError("secret"))
        == "Sync failed due to an internal error"
    )
    assert categorize_sync_error(RateLimitError()) == "rate_limited"
    assert categorize_sync_error(TransientError()) == "provider_unavailable"
    assert (
        categorize_sync_error(PermanentError("bad credential"))
        == "authentication"
    )
    assert (
        categorize_sync_error(PermanentError("security mapping"))
        == "data_mapping"
    )
    assert (
        categorize_sync_error(PermanentError("malformed payload"))
        == "validation"
    )
    assert categorize_sync_error(PermanentError("other")) == "unknown"
    assert categorize_sync_error(ConnectorError()) == "provider_unavailable"
    assert categorize_sync_error(RuntimeError("other")) == "unknown"
    database_error = type(
        "DatabaseError", (Exception,), {"__module__": "sqlalchemy.exc"}
    )()
    assert categorize_sync_error(database_error) == "database"
    assert categorize_export_error(None) is None
    assert categorize_export_error("401 credential error") == "authentication"
    assert categorize_export_error("429 rate limit") == "rate_limited"
    assert categorize_export_error("security mapping") == "data_mapping"
    assert categorize_export_error("invalid payload") == "validation"
    assert categorize_export_error("database unavailable") == "database"
    assert categorize_export_error("other failure") == "provider_unavailable"
    long_error = ConnectorError("x" * 3000)
    assert len(safe_sync_error_message(long_error)) == 2048


@pytest.mark.asyncio
async def test_exporter_retry_validates_existing_run_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import exporters

    container = SimpleNamespace(
        settings=SimpleNamespace(redis_url=None),
        redis_client=None,
    )
    monkeypatch.setattr(exporters, "get_container", lambda _request: container)
    auth = SimpleNamespace(tenant_id="tenant-1")
    run_id = "00000000-0000-0000-0000-000000000001"

    for run in (None, SimpleNamespace(status="completed")):
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    scalar_one_or_none=lambda run=run: run
                )
            )
        )
        with pytest.raises(HTTPException) as error:
            await exporters.retry_export_run(
                "wealthfolio", run_id, SimpleNamespace(), _auth=auth, db=db
            )
        assert error.value.status_code in {404, 409}


@pytest.mark.asyncio
async def test_lifespan_rejects_invalid_admin_key_and_skips_empty_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_sync import lifespan as lifespan_module

    conn = SimpleNamespace(execute=AsyncMock())
    container = SimpleNamespace(
        settings=SimpleNamespace(
            admin_key="invalid", is_production=True, is_staging=False
        ),
        engine=SimpleNamespace(
            begin=MagicMock(return_value=_AsyncContext(conn))
        ),
    )
    monkeypatch.setattr(lifespan_module, "_DB_RETRIES", 1)
    with pytest.raises(RuntimeError, match="exactly 32 characters"):
        await lifespan_module._init_database(container)

    no_candidates = SimpleNamespace(
        settings=SimpleNamespace(
            wealthfolio_password=None,
            wealthfolio_server_url=None,
            actual_budget_password=None,
            actual_budget_server_url=None,
        )
    )
    await lifespan_module._bootstrap_legacy_export_targets(no_candidates)

    tenant_missing = SimpleNamespace(
        settings=SimpleNamespace(
            wealthfolio_password="secret",
            wealthfolio_server_url="http://wealthfolio",
            actual_budget_password=None,
            actual_budget_server_url=None,
        ),
        session_factory=MagicMock(
            return_value=_AsyncContext(
                SimpleNamespace(scalar=AsyncMock(return_value=None))
            )
        ),
    )
    await lifespan_module._bootstrap_legacy_export_targets(tenant_missing)


@pytest.mark.asyncio
async def test_exporter_retry_returns_conflict_when_lease_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import exporters

    run = SimpleNamespace(status="failed")
    container = SimpleNamespace(
        settings=SimpleNamespace(redis_url="redis://localhost"),
        redis_client=object(),
    )
    monkeypatch.setattr(exporters, "get_container", lambda _request: container)
    monkeypatch.setattr(
        exporters,
        "retry_lease",
        lambda *_args, **_kwargs: _AsyncContext(
            SimpleNamespace(acquired=False)
        ),
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: run)
        )
    )
    with pytest.raises(HTTPException) as error:
        await exporters.retry_export_run(
            "wealthfolio",
            "00000000-0000-0000-0000-000000000001",
            SimpleNamespace(),
            _auth=SimpleNamespace(tenant_id="tenant-1"),
            db=db,
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_exporter_retry_locked_checks_run_presence_and_type() -> None:
    from fastapi import HTTPException

    from finance_sync.api.v1 import exporters

    settings = SimpleNamespace(
        exporter_wealthfolio_enabled=True,
        exporter_actual_budget_enabled=True,
        exporter_firefly_enabled=True,
    )
    container = SimpleNamespace(settings=settings)
    auth = SimpleNamespace(tenant_id="tenant-1")
    run_id = "00000000-0000-0000-0000-000000000001"

    for run, expected in (
        (None, 404),
        (SimpleNamespace(status="completed"), 409),
        (SimpleNamespace(status="failed", exporter_type="firefly"), 409),
    ):
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    scalar_one_or_none=lambda run=run: run
                )
            )
        )
        with pytest.raises(HTTPException) as error:
            await exporters._retry_export_run_locked(
                "wealthfolio",
                run_id,
                SimpleNamespace(),
                auth,
                db,
                SimpleNamespace(),
                container,
            )
        assert error.value.status_code == expected
