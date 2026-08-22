---
title: "Verwijder legacy security-resolutie uit orchestrator"
status: done
priority: 40
---

## Context

Security resolution is contractueel als dependency benoemd, maar de oude
`_resolve_security_reference`-methode staat nog in de orchestrator.

## Dependencies

Transaction/holding persistence cleanup en unresolved-security tests.

## Acceptance criteria

- [x] Verplaats of bevestig security resolution als zelfstandige resolver.
- [x] Verwijder `_resolve_security_reference` uit de orchestrator.
- [x] ISIN-first matching, ambiguity queue, manual resolution en unresolved
  metrics blijven gelijk.
- [x] Transaction-, holding- en security-resolutiontests slagen.

## Implementatie en verificatie

- `SecurityPersistence` is de zelfstandige resolver en wordt via
  `SyncPersistence` aan transaction- en holding-stages geïnjecteerd.
- De orchestrator bevat geen security-resolutiemethode; characterizationtests
  bewaken de grens.
- Verificatie: transaction-, holding-, resolver- en orchestrator-tests plus
  Ruff, Pyright en `git diff --check` geslaagd.
