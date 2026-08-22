---
title: "Verwijder legacy transaction- en holding-upserts uit orchestrator"
status: done
priority: 40
---

## Context

Concrete transaction- en holding-persistence bestaat, maar de oorspronkelijke
private upsertmethodes staan nog naast de nieuwe implementaties.

## Dependencies

`release13-remove-sync-legacy-account.md`.

## Acceptance criteria

- [x] Verwijder `_upsert_transaction` en `_upsert_holding` uit de orchestrator.
- [x] Verwijder alleen tests die uitsluitend private legacydetails testen;
  behoud gedragstests tegen de componenten.
- [x] Revisions, duplicate sync, snapshots, outbox en rollback blijven groen.
- [x] De orchestrator bevat geen `Transaction`- of `Holding`-constructie voor
  de generieke syncflow.

## Implementatie en verificatie

- Transaction- en holding-persistence blijft bij de concrete componenten in
  `sync/persistence.py`; de stages gebruiken de geïnjecteerde writer.
- De orchestrator bevat geen private upserts of entityconstructies; de
  boundary-test bewaakt dit blijvend.
- Verificatie: sync-, persistence-, duplicate-, snapshot-, outbox- en
  rollbacktests plus Ruff, Pyright en `git diff --check` geslaagd.
