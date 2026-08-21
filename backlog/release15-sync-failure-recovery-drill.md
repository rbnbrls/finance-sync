---
title: "Test sync-failure recovery en outbox-herstel"
status: todo
priority: 30
---

## Context

De syncflow is transactioneel ontworpen, maar periodiek bewijs van herstel na
een database-, worker- of outboxfout ontbreekt.

## Dependencies

Release 14 scheduled/card-persistence en release smoke-evidence.

## Acceptance criteria

- [ ] Injecteer fouten vóór commit, na domain writes en tijdens outbox-
  verwerking.
- [ ] Bewijs rollback, retrybaarheid en het ontbreken van halve succesvolle
  sync-runs.
- [ ] Bewijs dat een worker-restart geen dubbele outbox-uitkomst veroorzaakt.
- [ ] Leg herstelduur, retry-aantal en eindstatus vast in testoutput.
- [ ] Voer de drill uit tegen PostgreSQL en Redis in CI.
