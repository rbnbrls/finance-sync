# Upgrade guide

## Before upgrading

- Read the release notes and inspect the target image digest.
- Take a PostgreSQL backup and verify that it can be restored.
- Confirm that `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`,
  `MASTER_ENCRYPTION_KEY` and `CORS_ORIGINS` are present and valid.
- Check the current migration version with `alembic current`.
- Confirm that the deployment has a working health probe for both API and
  worker.

## Deployment order

The supported order is:

1. build and scan the immutable application image;
2. run `alembic upgrade head` as a one-shot migration job;
3. start or roll the API and worker to the same image;
4. verify `/health/live`, `/health/ready` and the worker health endpoint;
5. run the release smoke tests and inspect sync/export evidence.

Docker Compose implements this order with the `migrate` service dependency.
The application lifespan may seed default data, but it is not a schema
migration mechanism.

## Compatibility rules

Schema changes should use expand/contract sequencing. Add nullable columns,
tables or indexes first; deploy code that can read the old and new shape; only
remove old structures in a later release after the previous image is no
longer a rollback target. Keep migration `downgrade()` implementations
reversible where possible.

The current migration head is `0041_add_connection_test_metadata`. Use
`docs/MIGRATIONS.md` and the files under `migrations/versions/` as the source
of truth for revisions.

Release 13 follows backward-compatible expand/contract migrations. The normal
recovery action is an application-image rollback; production is never a blind
schema downgrade.

## Rollback

Prefer rolling back the application image while keeping the database at its
current revision. This is safe only when the migration followed the
expand/contract rule. Do not downgrade production automatically: first stop
traffic if necessary, take a backup, assess data loss, and rehearse the
downgrade on a restored copy.

If a migration is not backward-compatible, the release must provide an
explicit forward-fix or a tested rollback procedure in its release notes.
Release 13 closeout evidence includes the immutable image tag, migration
artifact link and staging smoke artifact link. Rollback remains an image rollback against backward-compatible migrations, never an automatic
production downgrade.

## Verification checklist

```bash
curl --fail "$API_URL/health/live"
curl --fail "$API_URL/health/ready"
curl --fail "$WORKER_URL/health/live"
alembic current
```

Then verify authentication, one connector test, one sync run, outbox/webhook
delivery if configured, and the configured destination export. Do not include
credentials or financial payloads in evidence artifacts.
