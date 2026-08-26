---
title: "Koppel read-querybudgets aan PostgreSQL-benchmarks"
status: done
priority: 30
---

## Context

Querybudgetten en `QueryCounter` bestaan, maar zijn nog niet gekoppeld aan
reproduceerbare PostgreSQL-datasets en CI-artifacts.

## Acceptance criteria

- [x] Maak deterministische fixtures voor 100 en 1.000 holdings, meerdere
  accounts en ontbrekende/stale prijzen.
- [x] Meet portfolio, holdings, securities, latest prices, net-worth en
  cashflow met `QueryCounter` tegen PostgreSQL.
- [x] Elke operatie verwijst naar een named budget uit
  `READ_QUERY_BUDGETS`.
- [x] Latest prices blijven één batch-query.
- [x] Een kunstmatige N+1-regressie laat de gate aantoonbaar falen.
- [x] Query count, latency, datasetgrootte en PostgreSQL/Python-versie worden
  als CI-artifact opgeslagen.

## Implementatie en verificatie

- `ReadBenchmarkResult` en `write_benchmark_report` leveren een JSON-artifact
  met querycount, latency, datasetgrootte en runtimeversies.
- `tests/integration/test_read_query_benchmarks_pg.py` seedt deterministisch
  100 en 1.000 holdings over respectievelijk 5 en 20 accounts, inclusief
  ontbrekende en stale prijzen.
- De benchmark meet alle zes read-operaties tegen PostgreSQL. De laatste
  prijzen worden via `fetch_latest_daily_prices` in één batchquery opgehaald.
- De benchmark bevat een expliciete N+1-regressietest die de
  `securities`-budgetgate laat falen bij per-security prijsqueries.
- De CI-integratiejob uploadt `read-benchmarks.json` als artifact.

Verificatie lokaal tegen PostgreSQL 16.15:

```text
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test \
TEST_REDIS_URL=redis://localhost:6380/15 \
READ_BENCHMARK_ARTIFACT=/tmp/read-benchmarks.json \
uv run pytest tests/integration/test_read_query_benchmarks_pg.py -m integration -q
2 passed

uv run pyright -p pyproject.toml src/finance_sync/services/read
0 errors

uv run pytest tests/test_release9_guards.py tests/test_release10_guards.py \
tests/test_read_facade_contract.py tests/test_read_analytics_cleanup.py -q
8 passed
```
