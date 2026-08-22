---
title: "Voer PostgreSQL-, Redis- en migration-gates uit"
status: done
priority: 40
---

## Context

Lokale unit-tests zijn groen, maar de formele PostgreSQL- en Redis-validatie
kan alleen tegen echte services worden uitgevoerd.

## Acceptance criteria

- [x] Draai op PostgreSQL 16 `upgrade head → downgrade base → upgrade head`.
- [x] Test migrations 0036 en 0037 op een lege database.
- [x] Draai integrationtests voor locks, webhook throttling, outbox,
  sync-idempotentie en persistence rollback tegen PostgreSQL 16/Redis 7.
- [x] Onverwachte integration-test-skips maken de CI-job rood.
- [x] JUnit-, migration- en service-logs worden als CI-artifacts opgeslagen.
- [x] De gate is reproduceerbaar via de bestaande CI-servicecontainers.

## Verification

- Local PostgreSQL 16 / Redis 7 run: `138 passed, 3150 deselected, 0 skipped`.
- Migration round-trip and migration tests for revisions 0036/0037 passed.
- Required JUnit reports now fail the job when a test is skipped or the report
  is missing; migration, integration and E2E logs are uploaded as artifacts.
