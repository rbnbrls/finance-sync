"""Backfill target-scoped identity on legacy Wealthfolio remediation items."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Wealthfolio target ids temporarily occupy the legacy connection_id
    # column; the credential FK would reject valid export-target ids.  Older
    # databases can carry either the naming-convention name or PostgreSQL's
    # default name, so discover the live constraint instead of assuming one.
    bind = op.get_bind()
    constraint_name = bind.execute(
        sa.text(
            """
            SELECT con.conname
            FROM pg_constraint AS con
            JOIN pg_class AS table_rel ON table_rel.oid = con.conrelid
            JOIN pg_class AS ref_rel ON ref_rel.oid = con.confrelid
            JOIN pg_attribute AS column_attr
              ON column_attr.attrelid = con.conrelid
             AND column_attr.attnum = con.conkey[1]
            JOIN pg_attribute AS ref_column_attr
              ON ref_column_attr.attrelid = con.confrelid
             AND ref_column_attr.attnum = con.confkey[1]
            WHERE con.contype = 'f'
              AND table_rel.relname = 'data_quality_remediation_items'
              AND ref_rel.relname = 'credentials'
              AND column_attr.attname = 'connection_id'
              AND ref_column_attr.attname = 'id'
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if constraint_name is not None:
        op.drop_constraint(
            constraint_name,
            "data_quality_remediation_items",
            type_="foreignkey",
        )
    # Only the bounded, explicitly persisted target_id is considered.  The
    # legacy exporter endpoint/account fields are deliberately not candidates
    # because they are mutable and can identify the wrong destination.
    op.execute(
        """
        WITH legacy AS (
            SELECT id, tenant_id, btrim(context->>'target_id') AS legacy_target_id
            FROM data_quality_remediation_items
            WHERE provider_key = 'wealthfolio'
              AND connection_id IS NULL
              AND jsonb_typeof(context->'target_id') = 'string'
              AND length(btrim(context->>'target_id')) BETWEEN 1 AND 128
        ), candidates AS (
            SELECT l.id, l.legacy_target_id,
                   count(et.id) AS candidate_count,
                   min(et.id::text) AS target_id
            FROM legacy l
            LEFT JOIN export_targets et
              ON et.tenant_id = l.tenant_id
             AND et.target_type = 'wealthfolio'
             AND (
                 et.id::text = l.legacy_target_id
                 OR et.configuration->>'legacy_target_id' = l.legacy_target_id
                 OR et.configuration->>'target_id' = l.legacy_target_id
             )
            GROUP BY l.id, l.legacy_target_id
        )
        UPDATE data_quality_remediation_items item
        SET connection_id = candidates.target_id::uuid,
            context = jsonb_set(
                item.context,
                '{target_id}',
                to_jsonb(candidates.target_id),
                true
            ),
            deduplication_key = encode(
                digest(
                    concat(
                        'dq:v1|tenant=', lower(btrim(item.tenant_id::text)),
                        '|connection=', lower(btrim(candidates.target_id)),
                        '|provider=', lower(btrim(item.provider_key)),
                        '|type=', lower(btrim(item.issue_type)),
                        '|entity=', lower(btrim(item.affected_entity_type)),
                        ':', item.affected_entity_id,
                        '|scope=', candidates.target_id
                    ),
                    'sha256'
                ),
                'hex'
            )
        FROM candidates
        WHERE item.id = candidates.id
          AND candidates.candidate_count = 1
        """
    )
    op.execute(
        """
        WITH legacy AS (
            SELECT id,
                   CASE
                       WHEN jsonb_typeof(context->'target_id') = 'string'
                        AND length(btrim(context->>'target_id')) BETWEEN 1 AND 128
                           THEN btrim(context->>'target_id')
                       ELSE NULL
                   END AS legacy_target_id
            FROM data_quality_remediation_items
            WHERE provider_key = 'wealthfolio'
              AND connection_id IS NULL
        ), candidates AS (
            SELECT l.id, count(et.id) AS candidate_count
            FROM legacy l
            JOIN data_quality_remediation_items item ON item.id = l.id
            LEFT JOIN export_targets et
              ON et.tenant_id = item.tenant_id
             AND et.target_type = 'wealthfolio'
             AND (
                 et.id::text = l.legacy_target_id
                 OR et.configuration->>'legacy_target_id' = l.legacy_target_id
                 OR et.configuration->>'target_id' = l.legacy_target_id
             )
            GROUP BY l.id
        )
        UPDATE data_quality_remediation_items item
        SET status = 'manual_review',
            last_error_category = CASE
                WHEN candidates.candidate_count > 1
                    THEN 'target_identity_ambiguous'
                ELSE 'target_identity_unresolved'
            END,
            context = item.context || jsonb_build_object(
                'manual_review', true,
                'reason', CASE
                    WHEN candidates.candidate_count > 1
                        THEN 'legacy_target_identity_ambiguous'
                    ELSE 'legacy_target_identity_unresolved'
                END
            )
        FROM candidates
        WHERE item.id = candidates.id
          AND candidates.candidate_count <> 1
        """
    )


def downgrade() -> None:
    # Target identity is intentionally not removed on downgrade: clearing it
    # would reintroduce cross-target remediation risk and lose audit evidence.
    pass
