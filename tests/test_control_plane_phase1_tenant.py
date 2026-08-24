"""Phase-1 tenant-boundary contract tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.exporter.models import ExportRun
from finance_sync.models.resolution_audit_log import ResolutionAuditLog
from finance_sync.models.unresolved_security import UnresolvedSecurity
from finance_sync.services.read_api import ReadService


def test_control_plane_records_have_tenant_ownership_columns() -> None:
    assert ExportRun.__table__.c.tenant_id.nullable is False
    assert UnresolvedSecurity.__table__.c.tenant_id.nullable is False
    assert ResolutionAuditLog.__table__.c.tenant_id.nullable is True


@pytest.mark.asyncio
async def test_sync_run_queries_add_tenant_predicate() -> None:
    session = AsyncMock()
    status_result = MagicMock()
    status_result.__iter__.return_value = iter([])
    total_result = MagicMock()
    total_result.scalar.return_value = 0
    items_result = MagicMock()
    items_result.scalars.return_value.all.return_value = []
    session.execute.side_effect = [status_result, total_result, items_result]

    await ReadService(session).list_sync_runs(tenant_id="tenant-a")

    statements = [str(call.args[0]) for call in session.execute.call_args_list]
    assert len(statements) == 3
    assert all("credentials.tenant_id" in statement for statement in statements)


def test_unresolved_security_uniqueness_includes_tenant() -> None:
    constraints = [
        constraint
        for constraint in UnresolvedSecurity.__table__.constraints
        if constraint.name == "uq_unresolved_provider_ext_id"
    ]

    assert len(constraints) == 1
    assert [column.name for column in constraints[0].columns] == [
        "tenant_id",
        "provider_key",
        "external_security_id",
    ]
