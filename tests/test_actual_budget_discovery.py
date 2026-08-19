"""Tests for Actual Budget budget discovery in the destination wizard.

Covers:
- ``_discover_budgets_sync`` maps ``list_user_files`` metadata into the
  wizard's budget list (id, sync_id, name, encrypted), skips deleted
  budgets and always cleans up the client.
- ``ActualBudgetClient.discover_budgets`` runs discovery in a worker
  thread and wraps failures as ``ActualBudgetConnectionError``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from finance_sync.exporter.actual_budget.client import (
    ActualBudgetClient,
    ActualBudgetConnectionError,
    _discover_budgets_sync,
)
from finance_sync.exporter.actual_budget.config import ActualBudgetConfig


def _fake_budget(
    *,
    file_id: str,
    group_id: str | None,
    name: str,
    deleted: bool,
    encrypted: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        file_id=file_id,
        group_id=group_id,
        name=name,
        deleted=deleted,
        encrypt_key_id="enc-key" if encrypted else None,
    )


def _fake_actual_module(
    budgets: list[SimpleNamespace],
) -> tuple[MagicMock, MagicMock]:
    """A fake ``actual`` module whose client lists *budgets*.

    Returns ``(module, client)`` where *client* is the instance the
    discovered ``Actual(...)`` call will return, so tests can assert on
    ``login`` / ``cleanup``.
    """
    module = MagicMock()
    client = MagicMock()
    client.login = MagicMock()
    client.cleanup = MagicMock()
    client.list_user_files.return_value = SimpleNamespace(data=budgets)
    module.Actual.return_value = client
    return module, client


def _config() -> ActualBudgetConfig:
    return ActualBudgetConfig(
        server_url="http://192.168.1.5:5006", password="secret"
    )


# ═══════════════════════════════════════════════════════════════════════
# _discover_budgets_sync
# ═══════════════════════════════════════════════════════════════════════


def test_discover_budgets_sync_maps_metadata_and_skips_deleted() -> None:
    budgets = [
        _fake_budget(
            file_id="budget-a",
            group_id="group-a",
            name="Home",
            deleted=False,
            encrypted=True,
        ),
        _fake_budget(
            file_id="budget-b",
            group_id="group-b",
            name="Work",
            deleted=False,
            encrypted=False,
        ),
        _fake_budget(
            file_id="budget-deleted",
            group_id="group-b",
            name="Old",
            deleted=True,
            encrypted=False,
        ),
    ]
    with patch.dict("sys.modules", {"actual": _fake_actual_module(budgets)[0]}):
        result = _discover_budgets_sync(_config())

    assert result == [
        {
            "id": "budget-a",
            "sync_id": "group-a",
            "name": "Home",
            "encrypted": True,
        },
        {
            "id": "budget-b",
            "sync_id": "group-b",
            "name": "Work",
            "encrypted": False,
        },
    ]
    # The deleted budget never appears in the wizard list.


def test_discover_budgets_uses_group_id_fallback_for_sync_id() -> None:
    budgets = [
        _fake_budget(
            file_id="solo-budget",
            group_id=None,
            name="Solo",
            deleted=False,
            encrypted=True,
        )
    ]
    with patch.dict("sys.modules", {"actual": _fake_actual_module(budgets)[0]}):
        result = _discover_budgets_sync(_config())
    # group_id is None → sync_id falls back to file_id.
    assert result[0]["sync_id"] == "solo-budget"


def test_discover_budgets_authenticates_and_cleans_up() -> None:
    budgets = [
        _fake_budget(
            file_id="b1",
            group_id="g1",
            name="B1",
            deleted=False,
            encrypted=False,
        )
    ]
    module, client = _fake_actual_module(budgets)
    with patch.dict("sys.modules", {"actual": module}):
        _discover_budgets_sync(_config())

    client.login.assert_called_once()
    client.list_user_files.assert_called_once()
    # Discovery never enters a budget context; it always cleans up.
    client.cleanup.assert_called_once()
    module.Actual.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# ActualBudgetClient.discover_budgets
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_discover_budgets_classmethod_runs_in_worker_thread() -> None:
    """The public discovery is a classmethod run in a worker thread; it
    must not need a connected client instance."""
    discovery = {
        "id": "budget-a",
        "sync_id": "group-a",
        "name": "Home",
        "encrypted": True,
    }
    with patch(
        "finance_sync.exporter.actual_budget.client._discover_budgets_sync",
        return_value=[discovery],
    ) as sync_mock:
        budgets = await ActualBudgetClient.discover_budgets(_config())

    sync_mock.assert_called_once_with(_config())
    assert budgets == [discovery]


@pytest.mark.asyncio
async def test_discover_budgets_wraps_failures_as_connection_error() -> None:
    with (
        patch(
            "finance_sync.exporter.actual_budget.client._discover_budgets_sync",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(ActualBudgetConnectionError, match="boom"),
    ):
        await ActualBudgetClient.discover_budgets(_config())
