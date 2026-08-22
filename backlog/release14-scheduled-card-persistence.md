---
title: "Breng scheduled-payment- en card-persistence onder een write-boundary"
status: done
priority: 30
---

## Context

De generieke account-, transaction- en holding-flow gebruikt concrete
persistence-componenten, maar scheduled payments en card transactions hebben
nog eigen upsertlogica in de orchestrator.

## Dependencies

Release 13 sync-legacy cleanup en persistence rollbacktests.

## Acceptance criteria

- [x] Maak typed persistence-operaties voor scheduled payments en card
  transactions.
- [x] Behoud provider-, connection-, tenant- en idempotentiescope.
- [x] Behoud change detection, revisions en outbox-events.
- [x] Laat de orchestrator alleen de flow en UnitOfWork beheren.
- [x] Test create, unchanged, changed, duplicate en rollback.

## Implementatie en verificatie

- `CardsPersistence` en `SyncPersistence` bieden typed scheduled-payment- en
  card-transaction-operaties; create/update emitten transactionele outbox-events.
- De orchestrator bevat geen modelconstructie voor deze records.
- Verificatie: sync-, card-, outbox- en boundary-tests plus Ruff, Pyright en
  `git diff --check` geslaagd.
- Tests: `tests/test_sync_orchestrator.py` en
  `tests/test_sync_orchestrator_boundary.py`.
- CI/artifact: transactionele outbox-events worden door de release-gates
  meegenomen in het service-gates artifact.
