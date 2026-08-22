---
title: "Verwijder legacy analytics-SQL uit ReadService"
status: done
priority: 40
---

## Context

Net-worth en cashflow delegeren al naar `AnalyticsReadService`; history-
varianten en oude analyticsblokken moeten nog worden opgeschoond.

## Dependencies

De analytics-readcomponenten en de read-facade cleanup.

## Acceptance criteria

- [x] Verwijder onbereikbare net-worth- en cashflowimplementaties.
- [x] Behoud tenant/account-scope, datumfilters, coverage en freshness.
- [x] Portfolio-history, net-worth-history en cashflow-history zijn expliciet
  gedelegeerd of als apart open item vastgelegd.
- [x] Analytics-, API- en OpenAPI-tests slagen.

## Verification

- Net-worth en cashflow worden uitsluitend door `AnalyticsReadService`
  uitgevoerd; de compatibility-facade bevat geen SQL of legacyblokken.
- History-methoden zijn expliciet ondergebracht in `OperationalReadService`,
  als voorbereiding op de geplande analytics-history component-story.
- Characterizationtest toegevoegd:
  `tests/test_read_analytics_cleanup.py`.
- Verificatie: `122 passed` voor analytics-, read-component-, API- en
  OpenAPI-tests; Ruff en `git diff --check` geslaagd.
