---
title: "Definieer autoscaling- en backpressurebeleid"
status: todo
priority: 20
---

## Context

Release 17 meet capaciteitslimieten, maar vertaalt die nog niet naar veilige
workerconcurrency, queue backpressure en deploymentlimieten.

## Dependencies

Release 17 capacity-limit tests en SLO-alerting.

## Acceptance criteria

- [ ] Leg limieten vast voor syncconcurrency, API requests, queue depth en
  database connections.
- [ ] Definieer backpressuregedrag en foutrespons bij overschrijding.
- [ ] Test autoscaling/burstscenario's met synthetische datasets.
- [ ] Bewijs dat tenant-isolatie en provider-rate limits behouden blijven.
- [ ] Documenteer aanbevolen minima, maxima en alertdrempels.
