from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


class _Session:
    def __init__(self, result: object) -> None:
        self.execute = AsyncMock(return_value=result)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Result:
    def scalars(self) -> _Result:
        return self

    def all(self) -> list[object]:
        return []


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session(_Result())


@pytest.mark.asyncio
async def test_ghostfolio_client_failure_is_captured() -> None:
    from finance_sync.exporter.ghostfolio import exporter as module
    from finance_sync.exporter.ghostfolio.config import GhostfolioConfig
    from finance_sync.exporter.ghostfolio.exporter import GhostfolioExporter

    failure = RuntimeError("Ghostfolio unavailable")
    service = GhostfolioExporter(
        _SessionFactory(), GhostfolioConfig(access_token="token"), "tenant-1"
    )
    client = SimpleNamespace(import_activities=AsyncMock(side_effect=failure))

    with (
        patch.object(module, "capture_connector_exception") as capture,
        pytest.raises(RuntimeError, match="Ghostfolio unavailable"),
    ):
        await service.run_export(client)

    capture.assert_called_once()
    assert capture.call_args.args[0] is failure
    assert capture.call_args.kwargs["connector"] == "ghostfolio"
    assert capture.call_args.kwargs["operation"] == "import_activities"


@pytest.mark.asyncio
async def test_investbrain_client_failure_is_captured() -> None:
    from finance_sync.exporter.investbrain import exporter as module
    from finance_sync.exporter.investbrain.config import InvestBrainConfig
    from finance_sync.exporter.investbrain.exporter import InvestBrainExporter

    failure = RuntimeError("InvestBrain unavailable")
    service = InvestBrainExporter(
        _SessionFactory(), InvestBrainConfig(access_token="token"), "tenant-1"
    )
    client = SimpleNamespace(list_portfolios=AsyncMock(side_effect=failure))

    with (
        patch.object(module, "capture_connector_exception") as capture,
        pytest.raises(RuntimeError, match="InvestBrain unavailable"),
    ):
        await service.run_export(client)

    capture.assert_called_once()
    assert capture.call_args.args[0] is failure
    assert capture.call_args.kwargs["connector"] == "investbrain"
    assert capture.call_args.kwargs["operation"] == "list_portfolios"


@pytest.mark.asyncio
async def test_securo_client_failure_returns_failed_result_and_is_captured(
    tmp_path: Path,
) -> None:
    from finance_sync.exporter.securo import exporter as module
    from finance_sync.exporter.securo.config import SecuroConfig
    from finance_sync.exporter.securo.exporter import SecuroExporter

    failure = RuntimeError("Securo unavailable")
    service = SecuroExporter(
        _SessionFactory(),
        SecuroConfig(output_dir=tmp_path),
        "tenant-1",
    )
    account = SimpleNamespace(
        id="account-1", name="Account", currency_code="EUR"
    )
    service._accounts = AsyncMock(return_value=[account])  # type: ignore[method-assign]
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.login = AsyncMock(side_effect=failure)

    with (
        patch.object(module, "SecuroClient", return_value=client),
        patch.object(module, "capture_connector_exception") as capture,
    ):
        result = await service.run_export(push=True)

    assert result.status == "failed"
    assert result.error_message == "Securo unavailable"
    capture.assert_called_once()
    assert capture.call_args.args[0] is failure
    assert capture.call_args.kwargs["connector"] == "securo"
    assert capture.call_args.kwargs["operation"] == "export"


@pytest.mark.asyncio
async def test_ynab_client_failure_is_captured() -> None:
    from finance_sync.exporter.ynab import exporter as module
    from finance_sync.exporter.ynab.config import YNABConfig
    from finance_sync.exporter.ynab.exporter import YNABExporter

    failure = RuntimeError("YNAB unavailable")
    service = YNABExporter(
        _SessionFactory(),
        YNABConfig(
            access_token="token",
            budget_id="budget-1",
            account_map={"account-1": "remote-account-1"},
        ),
        "tenant-1",
    )
    account = SimpleNamespace(id="account-1")
    transaction = SimpleNamespace(
        account_id="account-1",
        occurred_at=datetime.now(UTC),
        status="booked",
        cashflow_suggestion=None,
        counterparty_account_reference=None,
    )
    result = _Result()
    result.scalars = lambda: SimpleNamespace(all=lambda: [account])  # type: ignore[method-assign]
    transaction_result = _Result()
    transaction_result.scalars = lambda: SimpleNamespace(
        all=lambda: [transaction]
    )  # type: ignore[method-assign]

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def import_transactions(
            self, payload: list[dict[str, object]]
        ) -> None:
            raise failure

    with (
        patch.object(
            module,
            "YNABClient",
            return_value=Client(),
        ),
        patch.object(module, "capture_connector_exception") as capture,
        patch.object(
            module,
            "map_transaction",
            return_value={"import_id": "import-1"},
        ),
    ):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.execute = AsyncMock(side_effect=[result, transaction_result])
        session_factory = MagicMock(return_value=session)
        service._session_factory = session_factory
        with pytest.raises(RuntimeError, match="YNAB unavailable"):
            await service.run_export()

    capture.assert_called_once()
    assert capture.call_args.args[0] is failure
    assert capture.call_args.kwargs["connector"] == "ynab"
    assert capture.call_args.kwargs["operation"] == "import_transactions"
