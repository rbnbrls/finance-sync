---
title: "Breng scheduled-payment- en card-persistence onder een write-boundary"
status: done
priority: 30
---

## Context

Scheduled payments en card transactions gebruiken dezelfde typed persistence-
boundary als de overige sync-entiteiten.

## Acceptance criteria

- [x] Typed persistence-operaties voor scheduled payments en card transactions.
- [x] Provider-, connection-, tenant- en idempotentiescope blijft behouden.
- [x] Change detection, revisions en outbox-events blijven behouden.
- [x] De orchestrator beheert alleen flow en UnitOfWork.
- [x] Create, unchanged, changed, duplicate en rollback zijn getest.

## Implementatie en verificatie

- `CardsPersistence` en `SyncPersistence` bieden de typed operaties.
- Verificatie: sync-, card-, outbox- en boundary-tests geslaagd.
- Tests: `tests/test_sync_orchestrator.py` en boundary-tests.
- CI/artifact: transactionele outbox-events in het service-gates artifact.
