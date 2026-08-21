---
title: "Koppel read-querybudgets aan PostgreSQL-benchmarks"
status: todo
priority: 30
---

## Context

Querybudgetten en `QueryCounter` bestaan, maar zijn nog niet gekoppeld aan
reproduceerbare PostgreSQL-datasets en CI-artifacts.

## Acceptance criteria

- [ ] Maak deterministische fixtures voor 100 en 1.000 holdings, meerdere
  accounts en ontbrekende/stale prijzen.
- [ ] Meet portfolio, holdings, securities, latest prices, net-worth en
  cashflow met `QueryCounter` tegen PostgreSQL.
- [ ] Elke operatie verwijst naar een named budget uit
  `READ_QUERY_BUDGETS`.
- [ ] Latest prices blijven één batch-query.
- [ ] Een kunstmatige N+1-regressie laat de gate aantoonbaar falen.
- [ ] Query count, latency, datasetgrootte en PostgreSQL/Python-versie worden
  als CI-artifact opgeslagen.
