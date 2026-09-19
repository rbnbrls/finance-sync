from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.exporter.wealthfolio.client import WealthfolioAPIError
from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter


@pytest.mark.asyncio
async def test_profile_projection_failure_does_not_abort_export() -> None:
    """Unsupported optional profile APIs must not fail the destination run."""
    exporter = object.__new__(WealthfolioExporter)
    exporter._log = MagicMock()

    client = MagicMock()
    client.get_assets = AsyncMock(
        return_value=[{"id": "wf-asset", "displayCode": "ACME"}]
    )
    client.get_taxonomy = AsyncMock(return_value={})
    client.update_asset_profile = AsyncMock(
        side_effect=WealthfolioAPIError("HTTP 404: endpoint unavailable")
    )
    client.replace_asset_taxonomy_assignments = AsyncMock()

    security = SimpleNamespace(
        id="security-1",
        isin="US0000000001",
        ticker="ACME",
        name="Acme Inc.",
        security_type="stock",
    )

    await exporter._project_security_enrichment(
        client,
        {"security-1": security},
        {},
    )

    client.update_asset_profile.assert_awaited_once()
    exporter._log.warning.assert_called_once_with(
        "wealthfolio_asset_profile_update_failed",
        asset_id="wf-asset",
        error="HTTP 404: endpoint unavailable",
    )
