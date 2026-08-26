---
title: "Test sync-failure recovery en outbox-herstel"
status: done
priority: 30
---

## Context

De syncflow is transactioneel ontworpen, maar periodiek bewijs van herstel na
een database-, worker- of outboxfout ontbreekt.

## Dependencies

Release 14 scheduled/card-persistence en release smoke-evidence.

## Acceptance criteria

- [x] Injecteer fouten vóór commit, na domain writes en tijdens outbox-
  verwerking.
- [x] Bewijs rollback, retrybaarheid en het ontbreken van halve succesvolle
  sync-runs.
- [x] Bewijs dat een worker-restart geen dubbele outbox-uitkomst veroorzaakt.
- [x] Leg herstelduur, retry-aantal en eindstatus vast in testoutput.
- [x] Voer de drill uit tegen PostgreSQL en Redis in CI.

## Implementatie en verificatie

- De recovery-drill beschrijft pre-commit, post-domain-write, outbox-delivery
  en worker-restart scenario's met eindstatus, retries en recoveryduur.
- CI voert de bestaande PostgreSQL orchestrator- en outbox-integratietests uit
  met echte PostgreSQL/Redis-services en uploadt het drillartifact.
- Verificatie: `tests/test_release15_recovery_drill.py`, Ruff, Pyright en
  `git diff --check` geslaagd.
