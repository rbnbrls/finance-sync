---
title: "Verwijder legacy account-upsert uit orchestrator"
status: todo
priority: 40
---

## Context

`AccountPersistence` wordt door de pipeline gebruikt, maar de oude
`_upsert_account`-implementatie blijft in `SyncOrchestrator` staan.

## Dependencies

`release12-sync-orchestrator-legacy-cleanup.md` en account-persistence-tests.

## Acceptance criteria

- [ ] Verwijder `_upsert_account` en maak bestaande directe tests component-
  gericht.
- [ ] Account create/update/unchanged/outbox/owner-provenance blijft gelijk.
- [ ] De sync-orchestrator bevat geen account-modelconstructie meer.
- [ ] Sync-, persistence- en migration-tests slagen.
