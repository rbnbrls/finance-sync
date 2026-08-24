"""Unit tests for the phase-5 data-quality projection."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from finance_sync.services.data_quality import DataQualityService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


class _ScalarResult:
    def __init__(
        self,
        *,
        scalar: Any = None,
        scalars: list[Any] | tuple[Any, ...] = (),
        rows: list[Any] | tuple[Any, ...] = (),
    ) -> None:
        self._scalar = scalar
        self._scalars = list(scalars)
        self._rows = list(rows)

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalars(self) -> _ScalarResult:
        return self

    def __iter__(self) -> Iterator[Any]:
        return iter(self._scalars)

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(self, *responses: _ScalarResult) -> None:
        self._responses = list(responses)

    async def execute(self, _statement: Any) -> _ScalarResult:
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_empty_tenant_has_stable_unavailable_contract():
    session = _Session(
        _ScalarResult(scalar=None),
        _ScalarResult(rows=[]),
    )
    generated = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)

    overview = await DataQualityService(
        cast("AsyncSession", session), "tenant-a", now=generated
    ).get_overview()

    assert overview.status == "unavailable"
    assert overview.findings_total == 0
    assert overview.coverage == []
    assert overview.generated_at == generated


@pytest.mark.asyncio
async def test_overview_is_tenant_scoped_and_exposes_provenance_and_impact():
    run = SimpleNamespace(
        id="run-a",
        status="completed",
        started_at=datetime(2026, 8, 23, tzinfo=UTC),
        completed_at=datetime(2026, 8, 23, 0, 5, tzinfo=UTC),
    )
    finding = SimpleNamespace(
        id="finding-a",
        run_id="run-a",
        kind="duplicate_transaction",
        severity="warning",
        provider_key="bunq",
        other_provider_key="csv",
        account_id="account-a",
        transaction_id_a="tx-a",
        transaction_id_b="tx-b",
        external_transaction_id_a="external-a",
        external_transaction_id_b="external-b",
        description="Zelfde transactie lijkt tweemaal geïmporteerd.",
    )
    session = _Session(
        _ScalarResult(scalar=run),
        _ScalarResult(rows=[("bunq", 1, 2, run.started_at, run.completed_at)]),
        _ScalarResult(scalars=[finding]),
    )

    overview = await DataQualityService(
        cast("AsyncSession", session), "tenant-a"
    ).get_overview()

    assert overview.status == "attention_required"
    assert overview.findings_by_kind == {"duplicate_transaction": 1}
    issue = overview.issues[0]
    assert issue.transaction_ids == ["tx-a", "tx-b"]
    assert issue.external_record_ids == ["external-a", "external-b"]
    assert issue.impact_count == 2
    assert issue.action.path == "/api/v1/reconciliation/run-a"
    assert issue.action.permission == "reconciliation:read"
    assert "tenant-a" not in issue.description
