Integration reproducibility evidence
====================================

Target and checkout
-------------------
Target commit: abc3a77c5951fb2785f3a69ce55be925c5ecafd2
Target subject: feat: implement incident reporting for connector failures and add tests
Evidence target materialization: git archive into _repro_target/
Evidence-run worktree HEAD: 46784931ef5f4d664ac538047d1ae95e654a05c7

The integration commands were run from the archived target snapshot, not from
this worktree's descendant HEAD. No source files were changed by the run.

Host and runtime
----------------
Host/kernel: Linux hermesagent 7.0.14-14-pve #1 SMP PREEMPT_DYNAMIC PMX 7.0.14-14 (2026-08-22T15:01Z) x86_64 GNU/Linux
Python: 3.12.13 (via uv)
uv: 0.11.29 (x86_64-unknown-linux-gnu)
pytest: 9.1.1
Alembic: 1.19.1

Required real services and observed availability
-------------------------------------------------
Required by the documented Integration job:
- PostgreSQL image/version: postgres:16
- Redis image/version: redis:7
- CI service endpoints: PostgreSQL localhost:5432 and Redis localhost:6379
- Test URLs used: TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/finance_sync_test
  TEST_REDIS_URL=redis://localhost:6379/15

The host could not provide the required services. docker, docker compose,
psql, pg_isready, redis-server, and redis-cli were absent. Ports 5432, 5433,
6379, and 6380 were not listening. Consequently, PostgreSQL and Redis runtime
versions could not be obtained; the required versions above are the CI
configuration, not observed running-server versions. No mocked services were
used.

Setup evidence
--------------
Command: uv sync --extra dev --frozen
Exit: 0
Result: virtual environment created and 97 packages installed, including
asyncpg, redis, pytest, and Alembic.

Per attempt, the harness executed these capability checks:
- docker --version                         -> exit 127, docker: command not found
- docker image inspect postgres:16 ...     -> exit 127, docker: command not found
- docker image inspect redis:7 ...          -> exit 127, docker: command not found
- docker compose -f docker-compose.test.yml ps -> exit 127, docker: command not found
- docker compose -f docker-compose.test.yml exec -T postgres postgres --version
                                             -> exit 127, docker: command not found
- docker compose -f docker-compose.test.yml exec -T redis redis-server --version
                                             -> exit 127, docker: command not found

Reproduction transcript
-----------------------
Each of the following three attempts repeated the capability checks, then ran:

1. ASYNC_DB_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
   Attempt 1: exit 1; asyncpg refused ::1:5432 and 127.0.0.1:5432.
   Attempt 2: exit 1; asyncpg refused ::1:5432 and 127.0.0.1:5432.
   Attempt 3: exit 1; asyncpg refused ::1:5432 and 127.0.0.1:5432.

2. uv run pytest -m integration -v --junitxml=junit-integration.xml
   (The harness used an attempt-specific JUnit filename to avoid overwriting.)
   Attempt 1: exit 1; 3,978 collected, 3,801 deselected, 177 selected;
              177 errors; pytest duration 3.72s (measured 4427ms).
   Attempt 2: exit 1; 3,978 collected, 3,801 deselected, 177 selected;
              177 errors; pytest duration 3.64s (measured 4399ms).
   Attempt 3: exit 1; 3,978 collected, 3,801 deselected, 177 selected;
              177 errors; pytest duration 3.67s (measured 4374ms).

Follow-up commands:

3. uv run pytest tests/integration/test_read_query_benchmarks_pg.py -m integration -q
   Exit: 1; 2 setup errors in 0.64s (measured 992ms).

4. uv run python scripts/check_junit_no_skips.py junit-integration.xml
   Exit: 2; JUnit report is missing: junit-integration.xml.
   This is a harness naming deviation: the attempts wrote attempt-specific XML
   files, so the literal default filename did not exist.

5. DEBUG=false uv run python scripts/check_read_performance.py --baseline config/read-performance-baseline.json --current read-benchmarks.json --report read-performance-comparison.json
   Exit: 1; FileNotFoundError for read-benchmarks.json. The benchmark setup
   could not run because PostgreSQL was unavailable.

Outcome accounting
-------------------
Main integration repetitions: 3
Main integration: 0 passed, 0 failed assertions, 531 setup errors reported
(177 errors per run); all 3 process exits were 1.
Migration setup: 3 failures, all process exits 1.
Benchmark follow-up: 0 passed, 2 setup errors, process exit 1.
JUnit gate: setup/input failure, process exit 2; not a test failure.
Performance comparison: missing-input/setup failure, process exit 1; not a
performance test failure.

Observed stack trace and root cause
-----------------------------------
The first failing application-side setup path was tests/integration/conftest.py
pg_engine -> run_alembic("upgrade", "head", url=database_url). The captured
trace ends in asyncpg.connect_utils and asyncio.create_connection with:

OSError: Multiple exceptions: [Errno 111] Connect call failed ('::1', 5432, 0, 0), [Errno 111] Connect call failed ('127.0.0.1', 5432)

The same refusal appears in all three main logs and the benchmark follow-up.
The pytest errors are therefore fixture/setup propagation of an unavailable
PostgreSQL service. Redis tests were selected in the 177-test set, but the run
never reached meaningful Redis or application assertions because PostgreSQL
fixture setup failed first.

Classification and CI deviation
--------------------------------
The repeated result is deterministic for this host configuration: three of
three attempts failed at the same PostgreSQL setup boundary with the same
connection-refused error. This does not establish a deterministic application
failure, and application flakiness cannot be assessed without live PostgreSQL
16 and Redis 7. The target was not reproduced as a passing CI execution.

Documented CI procedure versus execution:
- CI declares postgres:16 and redis:7 service containers at localhost:5432 and
  localhost:6379; those containers could not be started or inspected here.
- CI runs against its checkout; this run used a git-archive target snapshot.
- CI's literal JUnit output name was requested, but the harness used
  attempt-specific XML names to prevent overwrites; consequently the later
  JUnit gate could not find junit-integration.xml.
- No mocked or substitute service was used.

Raw evidence and availability
-----------------------------
The execution worker reported these raw logs:
- integration-attempt-1.log
- integration-attempt-2.log
- integration-attempt-3.log
- integration-followup.log
- target-uv-sync.log

Original reported source directory:
/home/hermes/finance-sync-t_f5301ee2/.worktrees/t_0db110d9/
Setup note reported source:
/home/hermes/finance-sync-t_f5301ee2/.worktrees/t_1e7c02d6/integration-test-setup-abc3a77.md

Those original paths no longer exist in this workspace after worktree cleanup.
The report preserves the exact commands, exit codes, totals, durations, service
availability observations, and representative trace excerpt reported from the
captured run. Raw log contents are therefore unavailable for direct attachment;
this limitation is explicit and no uncaptured details are inferred.

Final classification
--------------------
Deterministic infrastructure/setup failure on this host (3/3 identical
PostgreSQL connection refusals). Application assertion failure, application
flakiness, and successful reproduction remain unassessable without PostgreSQL
16 and Redis 7. The benchmark, JUnit, and performance follow-ups failed due to
setup or missing-input conditions, not test assertions.
