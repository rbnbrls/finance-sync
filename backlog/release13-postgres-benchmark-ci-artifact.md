---
title: "Publiceer PostgreSQL query-benchmarkartifact"
status: todo
priority: 30
---

## Context

Querybudgetten en benchmarkprofielen bestaan lokaal, maar CI publiceert nog
geen reproduceerbaar benchmarkartifact tegen PostgreSQL.

## Dependencies

`release12-postgres-query-benchmarks.md`.

## Acceptance criteria

- [ ] Voeg een CI-stap toe die de PostgreSQL benchmarktests uitvoert.
- [ ] Upload query count, latency, datasetprofiel en databaseversie.
- [ ] Laat budgetoverschrijding de job falen.
- [ ] Houd latency informatief en query count de harde gate.
