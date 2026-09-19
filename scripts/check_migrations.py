#!/usr/bin/env python3
"""Validate the migration chain against the configured PostgreSQL database."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


EXPECTED_TABLES = (
    "export_runs",
    "export_deliveries",
    "ab_account_mappings",
    "wealthfolio_deliveries",
)


def run_alembic(*args: str) -> str:
    """Run Alembic and stop immediately when a migration command fails."""
    command = ["uv", "run", "alembic", *args]
    if args != ("history",):
        subprocess.run(command, check=True)
        return ""
    result = subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.stdout


async def verify_tables() -> None:
    """Verify the tables required by the export and remediation contracts."""
    database_url = os.environ["ASYNC_DB_URL"]
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            for table in EXPECTED_TABLES:
                result = await connection.execute(
                    text("SELECT to_regclass(:table_name)"),
                    {"table_name": f"public.{table}"},
                )
                if result.scalar() != table:
                    raise RuntimeError(f"missing table {table}")
                print(f"{table}: OK")
    finally:
        await engine.dispose()


def main() -> int:
    """Run the migration upgrade/downgrade round-trip and schema checks."""
    if not os.environ.get("ASYNC_DB_URL"):
        print(
            "ASYNC_DB_URL must point to a PostgreSQL database", file=sys.stderr
        )
        return 2

    history = run_alembic("history")
    revisions = [line for line in history.splitlines() if line.strip()]
    if sum("(head)" in line for line in revisions) != 1:
        raise RuntimeError("expected exactly one Alembic head")
    print(f"linear chain OK: {len(revisions)} revisions, single head")
    run_alembic("upgrade", "head")
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")
    asyncio.run(verify_tables())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
