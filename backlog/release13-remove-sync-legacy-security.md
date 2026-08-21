---
title: "Verwijder legacy security-resolutie uit orchestrator"
status: todo
priority: 40
---

## Context

Security resolution is contractueel als dependency benoemd, maar de oude
`_resolve_security_reference`-methode staat nog in de orchestrator.

## Dependencies

Transaction/holding persistence cleanup en unresolved-security tests.

## Acceptance criteria

- [ ] Verplaats of bevestig security resolution als zelfstandige resolver.
- [ ] Verwijder `_resolve_security_reference` uit de orchestrator.
- [ ] ISIN-first matching, ambiguity queue, manual resolution en unresolved
  metrics blijven gelijk.
- [ ] Transaction-, holding- en security-resolutiontests slagen.
