---
title: "Extraheer portfolio-, net-worth- en cashflow-history"
status: done
priority: 30
---

## Context

De belangrijkste analytics-reads zijn geëxtraheerd, maar history-varianten
staan nog in `read_api.py` en houden de facade groter dan de afgesproken grens.

## Dependencies

Release 13 read-legacy cleanup en analytics-tests.

## Acceptance criteria

- [x] Maak een zelfstandig analytics-history component voor portfolio history,
  net-worth history en cashflow history.
- [x] Behoud datumfilters, pagination, tenant/account-scope en responsevorm.
- [x] Laat `read_api.py` uitsluitend delegeren.
- [x] Voeg characterization- en componenttests toe.
- [x] Query budgets worden per history-operatie gecontroleerd.

## Implementatie en verificatie

- `AnalyticsHistoryReadService` is als expliciete facade-component toegevoegd;
  de bestaande querycontracten worden behouden.
- History-querybudgetten zijn toegevoegd voor portfolio, net-worth en
  cashflow-history.
- Verificatie: analytics-, read API-, OpenAPI- en Release 14-contracttests
  geslaagd; Ruff, Pyright en `git diff --check` geslaagd.
- Tests: `tests/test_release14_analytics_history.py` en
  `tests/test_read_analytics_cleanup.py`.
- CI/artifact: OpenAPI snapshots en diff-report uit de `openapi-diff`-job.
