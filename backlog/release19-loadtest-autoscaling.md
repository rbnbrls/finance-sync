---
title: "Valideer autoscaling met reproduceerbare loadtests"
status: done
priority: 20
---

## Context

Release 18 legt autoscaling en backpressurebeleid vast; de limieten moeten nog
tegen realistische API-, sync- en outboxbelasting worden gevalideerd.

## Dependencies

Release 18 capacity-autoscaling policy en Release 17 capacity-limit tests.

## Acceptance criteria

- [ ] Definieer loadprofielen voor API reads, sync runs, retries en outbox-
  consumers.
- [ ] Meet latency, error rate, queue depth, DB connections en worker count.
- [ ] Bewijs dat backpressure en provider-rate limits worden gerespecteerd.
- [ ] Laat overbelasting gecontroleerd falen zonder dubbele writes.
- [ ] Publiceer een baseline en schaaladvies zonder financiële data.
