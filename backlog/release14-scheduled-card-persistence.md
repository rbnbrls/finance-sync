---
title: "Breng scheduled-payment- en card-persistence onder een write-boundary"
status: todo
priority: 30
---

## Context

De generieke account-, transaction- en holding-flow gebruikt concrete
persistence-componenten, maar scheduled payments en card transactions hebben
nog eigen upsertlogica in de orchestrator.

## Dependencies

Release 13 sync-legacy cleanup en persistence rollbacktests.

## Acceptance criteria

- [ ] Maak typed persistence-operaties voor scheduled payments en card
  transactions.
- [ ] Behoud provider-, connection-, tenant- en idempotentiescope.
- [ ] Behoud change detection, revisions en outbox-events.
- [ ] Laat de orchestrator alleen de flow en UnitOfWork beheren.
- [ ] Test create, unchanged, changed, duplicate en rollback.
