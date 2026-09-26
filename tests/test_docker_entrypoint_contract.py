"""Regression tests for the container entrypoint contract.

`docker/entrypoint.sh` runs `alembic upgrade head` before the application
starts.  The migration environment (`migrations/env.py`) reads the database URL
from `ASYNC_DB_URL` / `DATABASE_URL` and raises
`RuntimeError("No database URL configured ...")` when neither is set.

Incident 2d643e85: the Coolify deployment of `financesync-test` had no database
URL configured at all, the entrypoint retried the permanently-failing migration
12 times (~60s) and aborted without naming the missing variable.  A missing URL
can never succeed, so it must fail immediately and say what is missing; a
*reachable-but-unready* database is the transient case the retry loop exists for
and must keep retrying.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"

# Variables `migrations/env.py` reads; the ambient test environment may carry
# them (the integration jobs set ASYNC_DB_URL), so every run starts from a
# scrubbed copy.
DATABASE_URL_VARS = ("ASYNC_DB_URL", "DATABASE_URL")


def _write_stubs(tmp_path: Path, *, fail_first: int) -> Path:
    """Create `alembic` and `sleep` stubs and return the directory to prepend.

    `alembic` fails `fail_first` times and then succeeds, recording every call.
    `sleep` is a no-op so the retry loop's 5s waits do not slow the suite down —
    the attempt count is asserted instead.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "alembic-calls.log"
    sleeps = tmp_path / "sleep-calls.log"

    alembic = stub_dir / "alembic"
    alembic.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{calls}"\n'
        f'attempts=$(wc -l < "{calls}")\n'
        f'if [ "$attempts" -le {fail_first} ]; then\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    sleep = stub_dir / "sleep"
    sleep.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{sleeps}"\nexit 0\n')
    for stub in (alembic, sleep):
        stub.chmod(0o755)
    return stub_dir


def _run_entrypoint(
    tmp_path: Path,
    *,
    fail_first: int = 0,
    env_overrides: Mapping[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], int, int]:
    """Run the entrypoint with stubbed tools; return result and call counts."""
    stub_dir = _write_stubs(tmp_path, fail_first=fail_first)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in DATABASE_URL_VARS and key != "SKIP_MIGRATIONS"
    }
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    env.update(env_overrides or {})

    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "/bin/true"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    alembic_calls = _count_lines(tmp_path / "alembic-calls.log")
    sleep_calls = _count_lines(tmp_path / "sleep-calls.log")
    return result, alembic_calls, sleep_calls


def _count_lines(path: Path) -> int:
    """Count recorded stub invocations; a stub never called leaves no file."""
    if not path.exists():
        return 0
    return len(path.read_text().splitlines())


class TestMissingDatabaseUrlFailsFast:
    """A permanent misconfiguration must abort immediately and name itself."""

    def test_no_database_url_aborts_without_migrating(
        self, tmp_path: Path
    ) -> None:
        result, alembic_calls, sleep_calls = _run_entrypoint(tmp_path)

        assert result.returncode == 1
        output = result.stdout + result.stderr
        assert "ASYNC_DB_URL" in output
        assert "DATABASE_URL" in output
        # The whole point: no migration attempt is made, so the failure is
        # immediate instead of 12 attempts plus ~60s of retries.
        assert alembic_calls == 0
        assert sleep_calls == 0
        assert "retrying in 5s" not in output

    def test_empty_values_count_as_unconfigured(self, tmp_path: Path) -> None:
        result, alembic_calls, _ = _run_entrypoint(
            tmp_path,
            env_overrides={"ASYNC_DB_URL": "", "DATABASE_URL": ""},
        )

        assert result.returncode == 1
        assert alembic_calls == 0

    def test_skip_migrations_bypasses_the_check(self, tmp_path: Path) -> None:
        result, alembic_calls, _ = _run_entrypoint(
            tmp_path, env_overrides={"SKIP_MIGRATIONS": "1"}
        )

        assert result.returncode == 0
        assert alembic_calls == 0
        assert "SKIP_MIGRATIONS=1" in result.stdout


class TestConfiguredDatabaseUrlStillMigrates:
    """Either variable, and the transient retry path, keep working."""

    def test_database_url_runs_migrations(self, tmp_path: Path) -> None:
        result, alembic_calls, sleep_calls = _run_entrypoint(
            tmp_path,
            env_overrides={
                "DATABASE_URL": "postgresql+asyncpg://u:p@postgres:5432/db"
            },
        )

        assert result.returncode == 0
        assert alembic_calls == 1
        assert sleep_calls == 0
        assert "alembic upgrade head completed" in result.stdout
        assert "starting: /bin/true" in result.stdout

    def test_async_db_url_runs_migrations(self, tmp_path: Path) -> None:
        result, alembic_calls, _ = _run_entrypoint(
            tmp_path,
            env_overrides={
                "ASYNC_DB_URL": "postgresql+asyncpg://u:p@postgres:5432/db"
            },
        )

        assert result.returncode == 0
        assert alembic_calls == 1

    def test_transient_failure_still_retries(self, tmp_path: Path) -> None:
        result, alembic_calls, sleep_calls = _run_entrypoint(
            tmp_path,
            fail_first=2,
            env_overrides={
                "DATABASE_URL": "postgresql+asyncpg://u:p@postgres:5432/db"
            },
        )

        assert result.returncode == 0
        assert alembic_calls == 3
        assert sleep_calls == 2
        assert "attempt 1/12" in result.stdout

    def test_repeated_failure_still_aborts_after_twelve_attempts(
        self, tmp_path: Path
    ) -> None:
        result, alembic_calls, sleep_calls = _run_entrypoint(
            tmp_path,
            fail_first=12,
            env_overrides={
                "DATABASE_URL": "postgresql+asyncpg://u:p@postgres:5432/db"
            },
        )

        assert result.returncode == 1
        assert alembic_calls == 12
        assert sleep_calls == 12
        assert "failed after 12 attempts" in result.stdout
