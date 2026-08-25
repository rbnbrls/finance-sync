# Database migrations

finance-sync uses Alembic for every PostgreSQL schema change. The application
does not create tables at runtime. Compose and the release pipeline run the
migration job before the API and worker start.

## Current state

The migration chain is linear and currently ends at:

```text
0040_add_recovery_metadata -> 0041_add_connection_test_metadata (head)
```

The complete chain is in `migrations/versions/`. Always inspect Alembic's
output rather than copying a revision list into another document:

```bash
ASYNC_DB_URL=postgresql+asyncpg://user:pass@host:5432/finance_sync \
  alembic heads
ASYNC_DB_URL=postgresql+asyncpg://user:pass@host:5432/finance_sync \
  alembic history
```

## Apply migrations

`migrations/env.py` reads `ASYNC_DB_URL`, falling back to `DATABASE_URL`, and
normalizes PostgreSQL URLs to the asyncpg driver.

```bash
ASYNC_DB_URL=postgresql+asyncpg://user:pass@host:5432/finance_sync \
  alembic upgrade head
```

In Docker Compose, the `migrate` service runs this command automatically. The
`app` and `worker` services depend on successful completion of that service.

For a fresh-database verification:

```bash
alembic upgrade head
alembic check
alembic downgrade base
```

Use a disposable PostgreSQL database for `downgrade base`.

## Add a migration

1. Confirm the current head with `alembic heads`.
2. Create one new revision with that head as `down_revision`:

   ```bash
   ASYNC_DB_URL=postgresql+asyncpg://user:pass@host:5432/finance_sync \
     alembic revision --autogenerate -m "describe the change"
   ```

3. Review the generated SQL and edit it when the change needs an explicit
   expand/contract sequence.
4. Implement a symmetric `downgrade()` unless the change is explicitly
   irreversible and documented in `UPGRADE.md`.
5. Run `upgrade head`, `alembic check` and the relevant tests on PostgreSQL.

Never edit or renumber a shipped revision. Keep the chain linear: one head,
one parent, and no merge revisions unless the release process explicitly
approves one.

## Safety rules

- Expand before contract so the previous image can still start during a
  rollback.
- Do not put secrets or financial payloads in migration logs or fixtures.
- Keep data backfills bounded and resumable where possible.
- Register new SQLAlchemy models in `src/finance_sync/models/__init__.py` so
  Alembic autogenerate can see them. Exporter models are loaded by
  `ensure_exporter_models_loaded()`.
- Keep seed/data migrations separate from schema-only migrations; see
  [SEED_MIGRATIONS.md](SEED_MIGRATIONS.md).

For deployment sequencing and rollback, see [UPGRADE.md](UPGRADE.md) and
[RELEASING.md](RELEASING.md).
