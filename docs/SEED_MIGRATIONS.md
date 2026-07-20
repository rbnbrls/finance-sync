# Seed / data migration pattern

## When to use

- **Seed migrations** pre-populate reference data (currencies, instrument types, tenant templates).
- **Data migrations** transform or backfill existing rows (e.g. compute `amount_in_base` from historic exchange rates).

Never put seed or data statements in a schema migration revision — they should be **separate, reversible scripts** so schema and data can be reasoned about independently.

## Recommended approach

### 1. Dedicated seed revision (idempotent)

Create a new Alembic revision with only data-ops:

```bash
ASYNC_DB_URL=postgresql+asyncpg://... alembic revision -m "seed_currencies"
```

Inside `upgrade()` use plain SQL or SQLAlchemy core, always wrapped in idempotent guards:

```python
def upgrade():
    op.execute("""
        INSERT INTO currencies (code, name, numeric_code)
        VALUES
            ('EUR', 'Euro', 978),
            ('USD', 'US Dollar', 840),
            ('GBP', 'British Pound', 826)
        ON CONFLICT (code) DO NOTHING
    """)
```

### 2. Standalone data migration script

For one-shot backfills that don't need rollback:

- Place the script under `scripts/data_migrations/YYYYMMDD_description.py`.
- Use the application's async session factory directly.
- Log progress in `sync_runs` or equivalent.
- **Remove the script after it has run in every environment** (or guard with an idempotency check).

Example structure:

```
scripts/
  data_migrations/
    20260720_backfill_transaction_base_amounts.py
```

### 3. Bootstrap seed in `lifespan.py`

For reference data that must exist before the app can serve requests (e.g. a default tenant), call a seed function inside the application's startup lifespan:

```python
async def seed_default_data(session_factory):
    async with session_factory() as session:
        existing = await session.get(Tenant, ...)
        if existing is None:
            session.add(Tenant(slug="default", name="Default Tenant"))
            await session.commit()
```

This is the **last resort** — prefer Alembic seeds for anything that should be tracked in version control.

## Rollback philosophy

- **Schema migrations**: always provide a `downgrade()`.
- **Seed migrations**: provide `downgrade()` that reverses the inserts (trivial for small reference sets; impractical for large backfills).
- **One-shot data scripts**: no downgrade — guard with idempotency checks instead. Document what was done in a commit message and remove the script post-execution.
