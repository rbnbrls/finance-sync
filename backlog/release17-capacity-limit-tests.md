---
title: "Meet capaciteitslimieten voor sync en read API"
status: done
priority: 20
---

## Context

Query- en latencybaselines meten regressie, maar de praktische grenzen voor
accounts, holdings, transacties en gelijktijdige workers zijn nog onbekend.

## Dependencies

Release 16 performance-monitoring en Release 15 failure-recovery drill.

## Acceptance criteria

- [x] Definieer deterministische datasets voor 100, 1.000 en 10.000 holdings
  en representatieve transacties.
- [x] Meet read latency, query count, syncduur, geheugen en outbox-lag.
- [x] Test ten minste één gelijktijdige worker en één rate-limited connector.
- [x] Leg soft/hard limits en aanbevolen deploymentconfiguratie vast.
- [x] Publiceer resultaten als CI/staging-artifact zonder financiële data.

## Implementatie en verificatie

- `config/capacity-limits.json` definieert 100/1.000/10.000 holdings,
  soft/hard limits en de aanbevolen API/sync-workerconfiguratie.
- `capacity_limit_report.py` maakt deterministische metingen voor read
  latency, query count, syncduur, geheugen en outbox-lag, inclusief twee
  concurrente workers en een rate-limited connector.
- CI archiveert `capacity-limits-${{ github.sha }}` zonder financiële waarden.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
