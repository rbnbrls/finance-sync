---
title: "Voer PostgreSQL-, Redis- en migration-gates uit"
status: todo
priority: 40
---

## Context

Lokale unit-tests zijn groen, maar de formele PostgreSQL- en Redis-validatie
kan alleen tegen echte services worden uitgevoerd.

## Acceptance criteria

- [ ] Draai op PostgreSQL 16 `upgrade head → downgrade base → upgrade head`.
- [ ] Test migrations 0036 en 0037 op een lege database.
- [ ] Draai integrationtests voor locks, webhook throttling, outbox,
  sync-idempotentie en persistence rollback tegen PostgreSQL 16/Redis 7.
- [ ] Onverwachte integration-test-skips maken de CI-job rood.
- [ ] JUnit-, migration- en service-logs worden als CI-artifacts opgeslagen.
- [ ] De gate is reproduceerbaar via de bestaande CI-servicecontainers.
