---
title: "Verwijder legacy account-upsert uit orchestrator"
status: done
priority: 40
---

## Context

`AccountPersistence` wordt door de pipeline gebruikt, maar de oude
`_upsert_account`-implementatie blijft in `SyncOrchestrator` staan.

## Dependencies

`release12-sync-orchestrator-legacy-cleanup.md` en account-persistence-tests.

## Acceptance criteria

- [x] Verwijder `_upsert_account` en maak bestaande directe tests component-
  gericht.
- [x] Account create/update/unchanged/outbox/owner-provenance blijft gelijk.
- [x] De sync-orchestrator bevat geen account-modelconstructie meer.
- [x] Sync-, persistence- en migration-tests slagen.

## Implementatie en verificatie

- Account persistence blijft eigendom van `AccountPersistence`; de
  orchestrator bevat geen account-upsert of modelconstructie.
- De componentgerichte tests en boundary-characterizationtest dekken create,
  update, unchanged, outbox en owner-provenance.
- Verificatie: sync-, persistence- en migration-tests plus Ruff, Pyright en
  `git diff --check` geslaagd.
