---
title: "Maak release-gates en smoke-resultaten operationeel zichtbaar"
status: todo
priority: 20
---

## Context

CI-artifacts bewijzen een release, maar operators hebben een compact overzicht
nodig van gate-status, sync-health, outbox-lag en laatste smoke-run.

## Dependencies

Release 14 release-smoke-evidence en backlog-closeout.

## Acceptance criteria

- [ ] Publiceer per releasecandidate een machineleesbare gate-summary.
- [ ] Neem unit, integration, E2E, migration, security, benchmark en staging
  op.
- [ ] Toon sync-health en outbox-lag zonder financiële waarden of secrets.
- [ ] Maak ontbrekende/te oude artifacts zichtbaar als failure.
- [ ] Documenteer waar operators logs, JUnit en scanrapporten vinden.
