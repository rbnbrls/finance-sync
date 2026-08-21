---
title: "Verwijder legacy analytics-SQL uit ReadService"
status: todo
priority: 40
---

## Context

Net-worth en cashflow delegeren al naar `AnalyticsReadService`; history-
varianten en oude analyticsblokken moeten nog worden opgeschoond.

## Dependencies

De analytics-readcomponenten en de read-facade cleanup.

## Acceptance criteria

- [ ] Verwijder onbereikbare net-worth- en cashflowimplementaties.
- [ ] Behoud tenant/account-scope, datumfilters, coverage en freshness.
- [ ] Portfolio-history, net-worth-history en cashflow-history zijn expliciet
  gedelegeerd of als apart open item vastgelegd.
- [ ] Analytics-, API- en OpenAPI-tests slagen.
