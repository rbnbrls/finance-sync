---
title: "Verwijder legacy transaction- en holding-upserts uit orchestrator"
status: todo
priority: 40
---

## Context

Concrete transaction- en holding-persistence bestaat, maar de oorspronkelijke
private upsertmethodes staan nog naast de nieuwe implementaties.

## Dependencies

`release13-remove-sync-legacy-account.md`.

## Acceptance criteria

- [ ] Verwijder `_upsert_transaction` en `_upsert_holding` uit de orchestrator.
- [ ] Verwijder alleen tests die uitsluitend private legacydetails testen;
  behoud gedragstests tegen de componenten.
- [ ] Revisions, duplicate sync, snapshots, outbox en rollback blijven groen.
- [ ] De orchestrator bevat geen `Transaction`- of `Holding`-constructie voor
  de generieke syncflow.
