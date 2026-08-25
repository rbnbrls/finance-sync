---
title: "Extraheer portfolio-, net-worth- en cashflow-history"
status: done
priority: 30
---

## Context

De analytics-history reads zijn ondergebracht in een afzonderlijke component
zodat de read-facade klein en controleerbaar blijft.

## Acceptance criteria

- [x] Portfolio-, net-worth- en cashflow-history gebruiken een eigen component.
- [x] Datumfilters, pagination, tenant/account-scope en responsevorm blijven behouden.
- [x] `read_api.py` delegeert uitsluitend.
- [x] Characterization- en componenttests zijn toegevoegd.
- [x] Query budgets worden per history-operatie gecontroleerd.

## Implementatie en verificatie

- `AnalyticsHistoryReadService` bevat de history-querycontracten en budgetten.
- Verificatie: analytics-, read API-, OpenAPI- en Release 14-tests geslaagd.
- Tests: `tests/test_release14_analytics_history.py` en read-analytics-tests.
- CI/artifact: OpenAPI snapshots en diff-report uit de `openapi-diff`-job.
