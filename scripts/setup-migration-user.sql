-- Migration database user setup
-- ===============================
-- Run this *once* as a PostgreSQL superuser (e.g. ``postgres``) to create
-- the ``finance_sync_migration`` role with just enough privileges to run
-- Alembic migrations (DDL), without granting the app user any schema
-- mutation privileges.
--
-- Usage
-- -----
--   psql -U postgres -d finance_sync -f scripts/setup-migration-user.sql
--
-- The resulting connection string for Alembic::
--
--   ASYNC_DB_URL=postgresql+asyncpg://finance_sync_migration:<PASSWORD>@localhost:5432/finance_sync
--
-- Security notes
-- --------------
-- 1. Change the password below to a strong, unique value.
-- 2. Store the password in your secrets manager / .env (never committed).
-- 3. The migration user owns only DDL privileges — the application user
--    (``finance_sync``) has DML-only grants.

-- ── Revoke anything that may have been granted by a previous run ─────
REVOKE ALL PRIVILEGES ON DATABASE finance_sync FROM finance_sync_migration;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM finance_sync_migration;
DROP OWNED BY finance_sync_migration;

-- ── Create the role (idempotent) ────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finance_sync_migration') THEN
        CREATE ROLE finance_sync_migration LOGIN PASSWORD 'change-me-to-a-strong-password';
    END IF;
END
$$;

-- ── Connect + schema ownership ──────────────────────────────────────
GRANT CONNECT ON DATABASE finance_sync TO finance_sync_migration;
GRANT USAGE, CREATE ON SCHEMA public TO finance_sync_migration;
ALTER DEFAULT PRIVILEGES FOR ROLE finance_sync_migration IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO finance_sync_migration;
ALTER DEFAULT PRIVILEGES FOR ROLE finance_sync_migration IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO finance_sync_migration;

-- ── Table-level DDL (migration user only) ───────────────────────────
-- The migration user can create / alter / drop any table in public.
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finance_sync_migration;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finance_sync_migration;

-- ── Application user (DML-only) ────────────────────────────────────
-- Run this separately if ``finance_sync`` already exists.
-- GRANT USAGE ON SCHEMA public TO finance_sync;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO finance_sync;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO finance_sync;
-- ALTER DEFAULT PRIVILEGES FOR ROLE finance_sync_migration IN SCHEMA public
--     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_sync;
-- ALTER DEFAULT PRIVILEGES FOR ROLE finance_sync_migration IN SCHEMA public
--     GRANT USAGE, SELECT ON SEQUENCES TO finance_sync;
