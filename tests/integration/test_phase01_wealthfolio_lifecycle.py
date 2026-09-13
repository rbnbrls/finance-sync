"""Persisted PostgreSQL lifecycle coverage for the Wealthfolio bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from finance_sync.models import (
    DataQualityRemediationItem,
    ExportTarget,
    Security,
    SecurityPrice,
    Tenant,
)
from finance_sync.reconciliation.remediation.executor import RemediationExecutor
from finance_sync.reconciliation.remediation.rate_limit import (
    QuotaPolicy,
    RemediationRateLimitCoordinator,
)
from finance_sync.reconciliation.remediation.wealthfolio import (
    WealthfolioQuoteStrategy,
)
from finance_sync.services.wealthfolio_health_bridge import (
    WealthfolioHealthBridge,
)

pytestmark = pytest.mark.integration


class FakeWealthfolioHttpClient:
    """Remote response double; lifecycle state remains in PostgreSQL."""

    QUOTE_DATA_SOURCE = "CUSTOM_SCRAPER:finance-sync"

    def __init__(self, *, remote_verification_succeeds: bool) -> None:
        self.remote_verification_succeeds = remote_verification_succeeds
        self.upsert_calls: list[tuple[str, dict[str, object]]] = []

    async def upsert_quote(
        self, asset_id: str, quote: dict[str, object]
    ) -> None:
        self.upsert_calls.append((asset_id, quote))

    async def get_quote_history(self, asset_id: str) -> list[dict[str, object]]:
        if not self.remote_verification_succeeds:
            return []
        return [
            {
                "assetId": asset_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "source": self.QUOTE_DATA_SOURCE,
            }
        ]


async def _seed_rows(session):
    tenant_id = str(uuid4())
    target_a = ExportTarget(
        tenant_id=tenant_id,
        target_type="wealthfolio",
        display_name="Wealthfolio A",
        status="active",
        configuration={"base_url": "https://wealthfolio-a.example.test"},
    )
    target_b = ExportTarget(
        tenant_id=tenant_id,
        target_type="wealthfolio",
        display_name="Wealthfolio B",
        status="active",
        configuration={"base_url": "https://wealthfolio-b.example.test"},
    )
    security = Security(
        isin="US0378331005",
        ticker="AAPL",
        name="Apple Inc.",
        security_type="stock",
        currency_code="USD",
    )
    session.add(
        Tenant(
            id=tenant_id,
            slug=f"wf-lifecycle-{tenant_id[:8]}",
            name="Wealthfolio lifecycle",
        )
    )
    await session.flush()
    session.add_all([target_a, target_b, security])
    await session.flush()
    session.add(
        SecurityPrice(
            security_id=str(security.id),
            timestamp=datetime.now(UTC),
            price_close=190,
            source="integration-fixture",
            interval="1d",
            currency_code="USD",
        )
    )
    await session.commit()
    return tenant_id, target_a, target_b, security


def _payload(security_id: str, remote_asset_id: str) -> dict[str, object]:
    return {
        "issues": [
            {
                "code": "MISSING_QUOTE",
                "severity": "error",
                "affectedItems": [
                    {"assetId": remote_asset_id, "securityId": security_id}
                ],
            }
        ]
    }


async def _fresh_item(session, tenant_id: str, target_id: str | None = None):
    query = select(DataQualityRemediationItem).where(
        DataQualityRemediationItem.tenant_id == tenant_id
    )
    if target_id:
        query = query.where(DataQualityRemediationItem.target_id == target_id)
    return (await session.execute(query)).scalars().all()


async def test_persisted_wealthfolio_poll_to_resolution_lifecycle(
    session_factory,
    redis_client,
) -> None:
    """Prove persisted idempotency, guards, target isolation, and verification ordering."""
    async with session_factory() as session:
        tenant_id, target_a, target_b, security = await _seed_rows(session)

    payload_a = _payload(str(security.id), "remote-asset-a")
    async with session_factory() as session:
        bridge = WealthfolioHealthBridge(session, tenant_id, target_a)
        first = await bridge.enqueue_success(
            payload_a, complete=True, compatibility_verified=True
        )
        second = await bridge.enqueue_success(
            payload_a, complete=True, compatibility_verified=True
        )
        assert first[0].id == second[0].id
        await session.commit()

    async with session_factory() as session:
        rows = await _fresh_item(session, tenant_id)
        assert len(rows) == 1
        assert str(rows[0].target_id) == str(target_a.id)
        await WealthfolioHealthBridge(
            session, tenant_id, target_a
        ).enqueue_success(
            {"issues": []},
            complete=False,
            truncated=True,
            cursor_state={"reason": "issue_limit"},
        )
        await session.commit()

    async with session_factory() as session:
        assert (await _fresh_item(session, tenant_id))[0].status == "pending"
        await WealthfolioHealthBridge(
            session, tenant_id, target_a
        ).record_failure(
            category="RequestError", message="remote poll unavailable"
        )
        await session.commit()

    async with session_factory() as session:
        item = (await _fresh_item(session, tenant_id))[0]
        assert item.status == "pending"
        claimed = await RemediationExecutor(session).backlog.claim(
            tenant_id=tenant_id, limit=1, lease_seconds=60
        )
        assert len(claimed) == 1
        await session.commit()

    settings = SimpleNamespace(
        openbb_api_key=None,
        openbb_base_url="https://openbb.example.test",
        openbb_request_timeout=5,
    )
    async with session_factory() as session:
        item = (await _fresh_item(session, tenant_id))[0]
        strategy = WealthfolioQuoteStrategy(session, settings)
        strategy.canonical.execute = AsyncMock()
        executor = RemediationExecutor(
            session,
            strategies={strategy.key: strategy},
            quota=RemediationRateLimitCoordinator(redis_client),
            quota_policies={strategy.key: QuotaPolicy(10, 60, "wealthfolio")},
            retry_base_seconds=1,
            retry_jitter=0,
        )
        client = FakeWealthfolioHttpClient(remote_verification_succeeds=False)
        assert await executor.execute(item, connector=client) == "retry_wait"
        await session.commit()

    async with session_factory() as session:
        item = (await _fresh_item(session, tenant_id))[0]
        assert item.status == "retry_wait"
        item.next_attempt_at = datetime.now(UTC)
        await session.commit()

    async with session_factory() as session:
        await WealthfolioHealthBridge(
            session, tenant_id, target_b
        ).enqueue_success(payload_a, complete=True, compatibility_verified=True)
        await session.commit()

    async with session_factory() as session:
        rows = await _fresh_item(session, tenant_id)
        assert len(rows) == 2
        assert {str(row.target_id) for row in rows} == {
            str(target_a.id),
            str(target_b.id),
        }
        item_a = next(
            row for row in rows if str(row.target_id) == str(target_a.id)
        )
        item_b = next(
            row for row in rows if str(row.target_id) == str(target_b.id)
        )
        assert item_a.status == "retry_wait"
        assert item_b.status == "pending"
        claimed = await RemediationExecutor(session).backlog.claim(
            tenant_id=tenant_id, limit=1, lease_seconds=60
        )
        assert [str(row.id) for row in claimed] == [str(item_a.id)]
        await session.commit()

    async with session_factory() as session:
        item_a = (await _fresh_item(session, tenant_id, str(target_a.id)))[0]
        strategy = WealthfolioQuoteStrategy(session, settings)
        strategy.canonical.execute = AsyncMock()
        executor = RemediationExecutor(
            session,
            strategies={strategy.key: strategy},
            quota=RemediationRateLimitCoordinator(redis_client),
            quota_policies={strategy.key: QuotaPolicy(10, 60, "wealthfolio")},
            retry_base_seconds=1,
            retry_jitter=0,
        )
        client = FakeWealthfolioHttpClient(remote_verification_succeeds=True)
        assert await executor.execute(item_a, connector=client) == "resolved"
        assert client.upsert_calls
        await session.commit()

    async with session_factory() as session:
        rows = await _fresh_item(session, tenant_id)
        assert (
            next(
                row for row in rows if str(row.target_id) == str(target_a.id)
            ).status
            == "resolved"
        )
        assert (
            next(
                row for row in rows if str(row.target_id) == str(target_b.id)
            ).status
            == "pending"
        )
