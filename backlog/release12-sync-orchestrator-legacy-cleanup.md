---
title: "Maak de sync-orchestrator uitsluitend coördinerend"
status: todo
priority: 40
---

## Context

Concrete account-, transaction- en holding-persistence wordt door
`sync/persistence.py` gebruikt, maar de oude `_upsert_*`- en
`_resolve_security_reference`-methodes staan nog in de orchestrator.

## Acceptance criteria

- [ ] Verwijder private entity-persistence uit `sync/orchestrator.py` nadat
  callers en characterizationtests naar de concrete componenten verwijzen.
- [ ] De orchestrator houdt alleen pipelineflow, cursors, run-status,
  logging/metrics en UnitOfWork-lifecycle.
- [ ] Transaction- en holding-stages gebruiken geen fallback naar private
  orchestrator-methodes.
- [ ] Create, changed update, unchanged update, duplicate sync, outbox en
  rollback blijven functioneel gelijk.
- [ ] De orchestrator heeft maximaal 900 regels.
- [ ] Sync-, outbox-, persistence- en integratietests zijn groen.
