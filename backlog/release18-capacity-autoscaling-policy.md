---
title: "Definieer autoscaling- en backpressurebeleid"
status: done
priority: 20
---

## Context

Release 17 meet capaciteitslimieten, maar vertaalt die nog niet naar veilige
workerconcurrency, queue backpressure en deploymentlimieten.

## Dependencies

Release 17 capacity-limit tests en SLO-alerting.

## Acceptance criteria

- [x] Leg limieten vast voor syncconcurrency, API requests, queue depth en
  database connections.
- [x] Definieer backpressuregedrag en foutrespons bij overschrijding.
- [x] Test autoscaling/burstscenario's met synthetische datasets.
- [x] Bewijs dat tenant-isolatie en provider-rate limits behouden blijven.
- [x] Documenteer aanbevolen minima, maxima en alertdrempels.

## Implementatie en verificatie

- `config/autoscaling-policy.json` definieert syncconcurrency 1–4, 100 API
  requests/sec, queue soft/hard 50/500 en maximaal 40 databaseconnecties.
- `autoscaling_policy.py` kiest accept, worker-scale/backpressure,
  provider-backoff, reject-with-retry-after of service-busy.
- Synthetische bursttests bewijzen tenant-isolatie en behoud van provider-rate
  limits; CI archiveert de beslissing.
- Verificatie: 3 tests, Ruff en Pyright geslaagd.
