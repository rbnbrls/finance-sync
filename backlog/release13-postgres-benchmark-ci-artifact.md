---
title: "Publiceer PostgreSQL query-benchmarkartifact"
status: done
priority: 30
---

## Context

Querybudgetten en benchmarkprofielen bestaan lokaal, maar CI publiceert nog
geen reproduceerbaar benchmarkartifact tegen PostgreSQL.

## Dependencies

`release12-postgres-query-benchmarks.md`.

## Acceptance criteria

- [x] Voeg een CI-stap toe die de PostgreSQL benchmarktests uitvoert.
- [x] Upload query count, latency, datasetprofiel en databaseversie.
- [x] Laat budgetoverschrijding de job falen.
- [x] Houd latency informatief en query count de harde gate.

## Implementatie en verificatie

- De PostgreSQL integration-job voert de deterministische benchmarktests uit
  met `READ_BENCHMARK_ARTIFACT` en uploadt `read-query-benchmarks`.
- Het JSON-artifact bevat PostgreSQL/Python-versie, datasetgrootte,
  query-count, budget en latency. `QueryBudget.assert_within` maakt query
  count de harde gate; latency blijft diagnostische informatie.
- Contract- en unittests toegevoegd in
  `tests/test_release13_benchmark_artifact.py`.
