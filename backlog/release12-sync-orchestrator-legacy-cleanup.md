---
title: "Maak de sync-orchestrator uitsluitend coördinerend"
status: done
priority: 40
---

## Context

Concrete account-, transaction- en holding-persistence wordt door
`sync/persistence.py` gebruikt, maar de oude `_upsert_*`- en
`_resolve_security_reference`-methodes staan nog in de orchestrator.

## Acceptance criteria

- [x] Verwijder private entity-persistence uit `sync/orchestrator.py` nadat
  callers en characterizationtests naar de concrete componenten verwijzen.
- [x] De orchestrator houdt alleen pipelineflow, cursors, run-status,
  logging/metrics en UnitOfWork-lifecycle.
- [x] Transaction- en holding-stages gebruiken geen fallback naar private
  orchestrator-methodes.
- [x] Create, changed update, unchanged update, duplicate sync, outbox en
  rollback blijven functioneel gelijk.
- [x] De orchestrator heeft maximaal 900 regels.
- [x] Sync-, outbox-, persistence- en integratietests zijn groen.

## Verification

- Account-, transaction-, holding-, security-, scheduled-payment- en
  card-transaction-persistence staat nu in concrete componenten onder
  `sync/persistence.py`.
- De cards-pipeline en result value objects zijn uit de orchestrator gehaald;
  de orchestrator bevat 879 regels en geen `_upsert_*`- of private security-
  persistence-methodes meer.
- Characterizationtest toegevoegd:
  `tests/test_sync_orchestrator_boundary.py`.
- Verificatie: `67 passed` voor sync/persistence/stage-tests, PostgreSQL/Redis
  integration `138 passed`, E2E `31 passed`, Ruff en `git diff --check` groen.
