---
title: "Definieer SLO's en alerts voor sync en outbox"
status: todo
priority: 20
---

## Context

Operationele observability toont gate-status, maar productiesignalen moeten
ook aangeven wanneer syncduur, failures of outbox-lag een SLO overschrijden.

## Dependencies

Release 15 operational observability en release rehearsal.

## Acceptance criteria

- [ ] Definieer SLO's voor sync-success rate, syncduur, outbox-lag en worker-
  failure rate.
- [ ] Voeg metrieklabels toe zonder tenant-ID's, credentials of financiële
  waarden.
- [ ] Maak alerts met severity, runbook-link en suppressie voor onderhoud.
- [ ] Test alerts met synthetische failures.
- [ ] Documenteer dashboards, ownership en escalation path.
