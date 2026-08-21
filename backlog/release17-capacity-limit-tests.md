---
title: "Meet capaciteitslimieten voor sync en read API"
status: todo
priority: 20
---

## Context

Query- en latencybaselines meten regressie, maar de praktische grenzen voor
accounts, holdings, transacties en gelijktijdige workers zijn nog onbekend.

## Dependencies

Release 16 performance-monitoring en Release 15 failure-recovery drill.

## Acceptance criteria

- [ ] Definieer deterministische datasets voor 100, 1.000 en 10.000 holdings
  en representatieve transacties.
- [ ] Meet read latency, query count, syncduur, geheugen en outbox-lag.
- [ ] Test ten minste één gelijktijdige worker en één rate-limited connector.
- [ ] Leg soft/hard limits en aanbevolen deploymentconfiguratie vast.
- [ ] Publiceer resultaten als CI/staging-artifact zonder financiële data.
