"""PostgreSQL coverage for the Wealthfolio remediation identity backfill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa

from tests.integration.conftest import run_alembic


async def test_migration_0072_backfills_unique_and_fails_closed_legacy_rows(
    session, database_url: str
) -> None:
    """Unique identity is linked; ambiguous and missing identity is manual-only."""
    await session.execute(sa.text("SELECT 1"))
    await session.rollback()
    run_alembic("downgrade", "0071", url=database_url)

    tenant_id = uuid4()
    unique_target_id = uuid4()
    ambiguous_a = uuid4()
    ambiguous_b = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, name, created_at, updated_at) "
            "VALUES (:id, :slug, :name, :created_at, :updated_at)"
        ),
        {
            "id": tenant_id,
            "slug": f"legacy-{tenant_id}",
            "name": "Legacy Wealthfolio",
            "created_at": now,
            "updated_at": now,
        },
    )
    for target_id, name, legacy_id in (
        (unique_target_id, "Unique", None),
        (ambiguous_a, "Ambiguous A", "legacy-ambiguous"),
        (ambiguous_b, "Ambiguous B", "legacy-ambiguous"),
    ):
        await session.execute(
            sa.text(
                "INSERT INTO export_targets "
                "(id, tenant_id, target_type, display_name, status, "
                "selected_account_ids, datasets, configuration, created_at, updated_at) "
                "VALUES (:id, :tenant_id, 'wealthfolio', :display_name, 'active', "
                "'[]'::jsonb, '[]'::jsonb, CAST(:configuration AS jsonb), :created_at, :updated_at)"
            ),
            {
                "id": target_id,
                "tenant_id": tenant_id,
                "display_name": name,
                "configuration": (
                    "{}"
                    if legacy_id is None
                    else '{"legacy_target_id": "legacy-ambiguous"}'
                ),
                "created_at": now,
                "updated_at": now,
            },
        )

    rows = (
        (uuid4(), str(unique_target_id), "pending"),
        (uuid4(), "legacy-ambiguous", "pending"),
        (uuid4(), "not-found", "pending"),
    )
    for index, (item_id, target_id, status) in enumerate(rows):
        await session.execute(
            sa.text(
                "INSERT INTO data_quality_remediation_items "
                "(id, tenant_id, provider_key, issue_type, affected_entity_type, "
                "affected_entity_id, severity, status, first_detected_at, "
                "last_seen_at, next_attempt_at, remediation_strategy, "
                "deduplication_key, context, created_at, updated_at) VALUES "
                "(:id, :tenant_id, 'wealthfolio', 'wealthfolio_quote_sync_failure', "
                "'wealthfolio_asset', :entity_id, 'warning', :status, :now, :now, :now, "
                "'wealthfolio_quote', :deduplication_key, CAST(:context AS jsonb), :now, :now)"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "entity_id": str(item_id),
                "status": status,
                "now": now + timedelta(seconds=index),
                "deduplication_key": str(item_id),
                "context": '{"target_id": "' + target_id + '"}',
            },
        )
    await session.commit()
    run_alembic("upgrade", "0072", url=database_url)

    result = await session.execute(
        sa.text(
            "SELECT target_id, status, last_error_category, context->>'reason' "
            "FROM data_quality_remediation_items ORDER BY created_at, id"
        )
    )
    migrated = list(result)
    assert migrated[0][0] == unique_target_id
    assert migrated[0][1] == "pending"
    assert migrated[1][0] is None
    assert migrated[1][1:] == (
        "manual_review",
        "target_identity_ambiguous",
        "legacy_target_identity_ambiguous",
    )
    assert migrated[2][0] is None
    assert migrated[2][1:] == (
        "manual_review",
        "target_identity_unresolved",
        "legacy_target_identity_unresolved",
    )
